# -*- coding: utf-8 -*-
# vim: set fileencoding=utf-8 sw=4 ts=4 et :
# Copyright (C) 2006-2011 CS-SI
# License: GNU GPL v2 <http://www.gnu.org/licenses/gpl-2.0.html>

"""
Ce module fournit les mécanismes permettant de traiter les messages
provenant du bus XMPP, afin que ceux-ci soient corrélés.

Il met également à disposition un moyen pour les règles de corrélation
d'émettre de nouveaux messages XML à destination du bus (par exemple,
des commandes pour Nagios).
"""

import time
from datetime import datetime

import transaction
from sqlalchemy import exc

from twisted.internet import defer, reactor, error
from twisted.python import threadpool
from wokkel.generic import parseXml
from lxml import etree

from vigilo.common.conf import settings

from vigilo.common.logging import get_logger
from vigilo.common.gettext import translate

from vigilo.connector.forwarder import PubSubSender
from vigilo.connector import MESSAGEONETOONE

from vigilo.models.session import DBSession
from vigilo.models.tables import SupItem, Version

from vigilo.pubsub.xml import namespaced_tag, NS_EVENT, NS_TICKET, \
                                NS_COMPUTATION_ORDER
from vigilo.correlator.actors import executor
from vigilo.correlator.context import Context
from vigilo.correlator.handle_ticket import handle_ticket
from vigilo.correlator.db_insertion import insert_event, insert_state, \
                                    insert_hls_history, OldStateReceived, \
                                    NoProblemException
from vigilo.correlator.publish_messages import publish_state
from vigilo.correlator.correvent import make_correvent
from vigilo.correlator import registry

LOGGER = get_logger(__name__)
_ = translate(__name__)

def extract_information(payload):
    """
    Extrait les informations d'un message, en le parcourant
    une seule fois afin d'optimiser les performances.
    """

    info_dictionary = {"host": None,
                       "service": None,
                       "state": None,
                       "timestamp": None,
                       "message": None,
                       "impacted_HLS": None,
                       "ticket_id": None,
                       "acknowledgement_status": None,}

    # @TODO: spécifier explicitement le(s) XMLNS au(x)quel(s) on s'attend.
    # Récupération du namespace utilisé
    namespace = payload.nsmap[payload.prefix]

    for element in payload.getchildren():
        for tag in info_dictionary.keys():
            if element.tag == namespaced_tag(namespace, tag):
                if not element.text is None:
                    if element.tag == namespaced_tag(namespace, "timestamp"):
                        try:
                            info_dictionary["timestamp"] = \
                                datetime.fromtimestamp(int(element.text))
                        except ValueError:
                            info_dictionary["timestamp"] = datetime.now()
                    else:
                        info_dictionary[tag] = u'' + element.text

    if info_dictionary["host"] == settings['correlator']['nagios_hls_host']:
        info_dictionary["host"] = None

    return info_dictionary

class RuleDispatcher(PubSubSender):
    """
    Cette classe corrèle les messages reçus depuis le bus XMPP
    et envoie ensuite les résultats sur le bus.
    """

    _context_factory = Context

    def __init__(self, database):
        super(RuleDispatcher, self).__init__()
        self.max_send_simult = 1
        self._process_as_domish = False
        self.tree_end = None
        self._database = database

        # Préparation du pool d'exécuteurs de règles.
        timeout = settings['correlator'].as_int('rules_timeout')
        if timeout <= 0:
            timeout = None

        min_runner = settings['correlator'].as_int('min_rule_runners')
        max_runner = settings['correlator'].as_int('max_rule_runners')

        try:
            max_idle = settings['correlator'].as_int('rule_runners_max_idle')
        except KeyError:
            max_idle = 20

        # @TODO: #875: réimplémenter le timeout avec des threads.
        self.rrp = threadpool.ThreadPool(min_runner, max_runner, "Rule runners")
        LOGGER.debug("Starting rule runners")
        self.rrp.start()
        self._executor = executor.Executor(self)
        self._tmp_correl_time = None
        self._correl_times = []

    def check_database_connectivity(self):
        def _db_request():
            """
            Requête SQL élémentaire afin de vérifier
            la connectivité avec la base de données.
            """
            return Version.by_object_name('vigilo.models')

        # Évite de boucler sur une erreur si la base de données
        # n'est pas disponible au lancement du corrélateur.
        d = self._database.run(_db_request)

        def no_database(failure):
            """
            Méthode appelée lorsque la connexion avec la base de données
            ne peut être établie.
            """
            LOGGER.error(_("Unable to contact the database: %s"),
                failure.getErrorMessage())
            try:
                reactor.stop()
            except error.ReactorNotRunning:
                pass
        d.addErrback(no_database)

    def _putResultInDeferred(self, deferred, f, args, kwargs):
        d = defer.maybeDeferred(f, *args, **kwargs)
        d.addCallbacks(
            lambda res: reactor.callFromThread(deferred.callback, res),
            lambda fail: reactor.callFromThread(deferred.errback, fail),
        )

    def doWork(self, f, *args, **kwargs):
        """
        Délègue le travail aux threads dédiés à la corrélation.
        """
        d = defer.Deferred()
        self.rrp.callInThread(self._putResultInDeferred, d, f, args, kwargs)
        return d

    def itemsReceived(self, event):
        """
        Méthode appelée lorsque des éléments ont été reçus depuis
        le bus XMPP.

        @param event: Événement XMPP reçu.
        @type event: C{twisted.words.xish.domish.Element}
        """
        for item in event.items:
            # Item is a domish.IElement and a domish.Element
            # Serialize as XML before queueing,
            # or we get harmless stderr pollution  × 5 lines:
            # Exception RuntimeError: 'maximum recursion depth exceeded in
            # __subclasscheck__' in <type 'exceptions.AttributeError'> ignored
            #
            # stderr pollution caused by http://bugs.python.org/issue5508
            # and some touchiness on domish attribute access.
            xml = item.toXml()
            if item.name != 'item':
                # The alternative is 'retract', which we silently ignore
                # We receive retractations in FIFO order,
                # ejabberd keeps 10 items before retracting old items.
                LOGGER.debug(_(u'Skipping unrecognized item (%s)'), item.name)
                continue
            self.forwardMessage(xml)

    def _processException(self, failure):
        if not failure.check(KeyboardInterrupt):
            LOGGER.error(_('Unexpected error: %s'), failure.getErrorMessage())
        return failure

    def processMessage(self, xml):
        res = defer.maybeDeferred(self._processMessage, xml)
        res.addErrback(self._processException)
        return res

    def _processMessage(self, xml):
        """
        Transfère un message XML sérialisé vers la file.

        @param xml: message XML à transférer.
        @type xml: C{str}
        @return: Un objet C{Deferred} correspondant au traitement
            du message par les règles de corrélation ou C{None} si
            le message n'a pas pu être traité (ex: message invalide).
        @rtype: C{twisted.internet.defer.Deferred} ou C{None}
        """
        dom = etree.fromstring(xml)

        # Extraction de l'id XMPP.
        # Note: dom['id'] ne fonctionne pas dans lxml, dommage.
        idxmpp = dom.get('id')
        if idxmpp is None:
            LOGGER.error(_("Received invalid XMPP item ID (None)"))
            return defer.succeed(None)

        # Ordre de calcul de l'état d'un service de haut niveau.
        if dom[0].tag == namespaced_tag(NS_COMPUTATION_ORDER,
                                        'computation_order'):
            return self._computation_order(dom, xml, idxmpp)

        # Extraction des informations du message
        info_dictionary = extract_information(dom[0])

        # S'il s'agit d'un message concernant un ticket d'incident :
        if dom[0].tag == namespaced_tag(NS_TICKET, 'ticket'):
            d = self._do_in_transaction(
                _("Error while modifying the ticket"),
                xml, [exc.OperationalError],
                handle_ticket, info_dictionary,
            )
            return d

        # Sinon, s'il ne s'agit pas d'un message d'événement (c'est-à-dire
        # un message d'alerte de changement d'état), on ne le traite pas.
        if dom[0].tag != namespaced_tag(NS_EVENT, 'event'):
            return defer.succeed(None)

        idsupitem = self._do_in_transaction(
            _("Error while retrieving supervised item ID"),
            xml, [exc.OperationalError],
            SupItem.get_supitem,
            info_dictionary['host'],
            info_dictionary['service']
        )
        idsupitem.addCallback(self._finalizeInfo,
            idxmpp,
            dom, xml,
            info_dictionary
        )
        return idsupitem

    def _computation_order(self, dom, xml, idxmpp):
        if 'HighLevelServiceDepsRule' not in \
            registry.get_registry().rules.keys():
            LOGGER.warning(_("The rule 'vigilo.correlator_enterprise."
                            "rules.hls_deps:HighLevelServiceDepsRule' "
                            "must be loaded for computation orders to "
                            "be handled properly."))
            return defer.succeed(None)

        rule = registry.get_registry().rules.lookup('HighLevelServiceDepsRule')

        def eb(failure):
            if failure.check(defer.TimeoutError):
                LOGGER.info(_("The connection to memcached timed out. "
                                "The message will be handled once more."))
                self.queue.append(xml)
                return # Provoque le retraitement du message.
            return failure

        ctx = self._context_factory(idxmpp)
        hls_names = set()
        for child in dom[0].iterchildren():
            servicename = child.text
            if not isinstance(servicename, unicode):
                servicename = servicename.decode('utf-8')
            hls_names.add(servicename)

        hls_names = list(hls_names)
        d = ctx.set('impacted_hls', hls_names)
        d.addCallback(lambda _dummy: ctx.set('hostname', None))
        d.addCallback(lambda _dummy: ctx.set('servicename', None))
        d.addErrback(eb)
        d.addCallback(lambda _dummy: \
            self.doWork(
                rule.compute_hls_states,
                self, idxmpp,
                None, None,
                hls_names
            )
        )
        return d


    def _finalizeInfo(self, idsupitem, idxmpp, dom, xml, info_dictionary):
        # Ajoute l'identifiant du SupItem aux informations.
        info_dictionary['idsupitem'] = idsupitem

        # On initialise le contexte et on y insère
        # les informations sur l'alerte traitée.
        ctx = self._context_factory(idxmpp)

        attrs = {
            'hostname': 'host',
            'servicename': 'service',
            'statename': 'state',
            'timestamp': 'timestamp',
            'idsupitem': 'idsupitem',
        }

        d = defer.Deferred()

        def prepare_ctx(res, ctx_name, value):
            return ctx.set(ctx_name, value)
        def eb(failure):
            if failure.check(defer.TimeoutError):
                LOGGER.info(_("The connection to memcached timed out. "
                                "The message will be handled once more."))
                self.queue.append(xml)
                return # Provoque le retraitement du message.
            return failure

        for ctx_name, info_name in attrs.iteritems():
            d.addCallback(prepare_ctx, ctx_name, info_dictionary[info_name])

        # Dans l'ordre :
        # - On enregistre l'état correspondant à l'événement.
        # - On insère une entrée d'historique pour l'événement.
        # - On réalise la corrélation.
        d.addCallback(self._insert_state, info_dictionary, xml)
        d.addCallback(self._insert_history, info_dictionary, idxmpp, dom, ctx, xml)
        d.addErrback(eb)
        d.callback(None)
        return d

    def _insert_state(self, result, info_dictionary, xml):
        LOGGER.debug(_('Inserting state'))
        d = self._do_in_transaction(
            _("Error while saving state"),
            xml, [exc.OperationalError],
            insert_state, info_dictionary
        )
        return d

    def _insert_history(self, previous_state, info_dictionary, idxmpp, dom, ctx, xml):
        if isinstance(previous_state, OldStateReceived):
            LOGGER.debug("Ignoring old state for host %(host)s and service "
                         "%(srv)s (current is %(cur)s, received %(recv)s)"
                         % {"host": info_dictionary["host"],
                            "srv": info_dictionary["service"],
                            "cur": previous_state.current,
                            "recv": previous_state.received,
                            }
                         )
            return # on arrête le processus ici
        # On insère le message dans la BDD, sauf s'il concerne un HLS.
        if not info_dictionary["host"]:
            LOGGER.debug(_('Inserting an entry in the HLS history'))
            d = self._do_in_transaction(
                _("Error while adding an entry in the HLS history"),
                xml, [exc.OperationalError],
                insert_hls_history, info_dictionary
            )
        else:
            LOGGER.debug(_('Inserting an entry in the history'))
            d = self._do_in_transaction(
                _("Error while adding an entry in the history"),
                xml, [exc.OperationalError],
                insert_event, info_dictionary
            )

        def commit(res):
            transaction.commit()
            return res
        d.addCallback(commit)
        d.addCallback(self._do_correl, previous_state, info_dictionary,
                                       idxmpp, dom, xml, ctx)

        def no_problem(fail):
            """
            Court-circuite l'exécution des règles de corrélation
            lorsqu'aucun événement corrélé n'existe en base de données
            et qu'on reçoit un message indiquant un état nominal (OK/UP).
            """
            if fail.check(NoProblemException):
                self._messages_sent += 1
                return None
            return fail
        d.addErrback(no_problem)
        return d

    def _do_correl(self, raw_event_id, previous_state, info_dictionary,
                   idxmpp, dom, xml, ctx):
        LOGGER.debug(_('Actual correlation'))

        d = defer.Deferred()
        d.addCallback(lambda result: ctx.set('payload', xml))
        d.addCallback(lambda result: ctx.set('previous_state', previous_state))

        if raw_event_id:
            d.addCallback(lambda result: ctx.set('raw_event_id', raw_event_id))

        d.addCallback(lambda result: etree.tostring(dom[0]))

        def start_correl(payload, defs):
            tree_start, self.tree_end = defs

            def send(res):
                sr = self._send_result(res, xml, info_dictionary)
                sr.addErrback(self._send_result_eb, idxmpp, payload)
                return sr

            # Gère les erreurs détectées à la fin du processus de corrélation,
            # ou émet l'alerte corrélée s'il n'y a pas eu de problème.
            self.tree_end.addCallbacks(
                send,
                self._correlation_eb,
                errbackArgs=[idxmpp, payload],
            )

            # On lance le processus de corrélation.
            self._tmp_correl_time = time.time()
            tree_start.callback(idxmpp)
            return self.tree_end
        d.addCallback(start_correl, self._executor.build_execution_tree())

        def end(result):
            duration = time.time() - self._tmp_correl_time
            self._correl_times.append(duration)
            LOGGER.debug(_('Correlation process ended (%.4fs)'), duration)
            return result
        d.addCallback(end)
        d.callback(None)
        return d

    def _send_result(self, result, xml, info_dictionary):
        """
        Traite le résultat de l'exécution de TOUTES les règles
        de corrélation.

        @param result: Résultat de la corrélation (transmis
            automatiquement par Twisted, vaut toujours None
            chez nous).
        @type result: C{None}
        @param xml: Message XML sérialisé traité par la corrélation.
        @type xml: C{unicode}
        @param info_dictionary: Informations extraites du message XML.
        @param info_dictionary: C{dict}
        """
        LOGGER.debug(_('Handling correlation results'))

        # On publie sur le bus XMPP l'état de l'hôte
        # ou du service concerné par l'alerte courante.
        publish_state(self, info_dictionary)

        d = defer.Deferred()
        def inc_messages(result):
            self._messages_sent += 1
        d.addCallback(inc_messages)

        dom = etree.fromstring(xml)
        idnt = dom.get('id')

        # Pour les services de haut niveau, on s'arrête ici,
        # on NE DOIT PAS générer d'événement corrélé.
        if info_dictionary["host"] == settings['correlator']['nagios_hls_host']:
            d.callback(None)
            return d

        dom = dom[0]
        def cb(result, *args, **kwargs):
            return make_correvent(self, self._database, *args, **kwargs)
        def eb(failure, xml):
            LOGGER.info(_(
                'Error while saving the correlated event (%s). '
                'The message will be handled once more.'),
                str(failure).decode('utf-8')
            )
            self.queue.append(xml)
            return None
        d.addCallback(lambda res: self._database.run(
            transaction.begin, transaction=False))
        d.addCallback(cb, dom, idnt, info_dictionary)
        d.addCallback(lambda res: self._database.run(
            transaction.commit, transaction=False))
        d.addErrback(eb, xml)

        d.callback(None)
        return d

    def _correlation_eb(self, failure, idxmpp, payload):
        """
        Cette méthode est appelée lorsque la corrélation échoue.
        Elle se contente d'afficher un message d'erreur.

        @param failure: L'erreur responsable de l'échec.
        @type failure: C{Failure}
        @param idxmpp: Identifiant du message XMPP.
        @type idxmpp: C{str}
        @param payload: Le message reçu à corréler.
        @type payload: C{Element}
        @return: L'erreur reponsable de l'échec.
        @rtype: C{Failure}
        """
        LOGGER.error(_('Correlation failed for '
                        'message #%(id)s (%(payload)s)'), {
            'id': idxmpp,
            'payload': payload,
        })
        return failure

    def _send_result_eb(self, failure, idxmpp, payload):
        """
        Cette méthode est appelée lorsque la corrélation
        s'est bien déroulée mais que le traitement des résultats
        a échoué.
        Elle se contente d'afficher un message d'erreur.

        @param failure: L'erreur responsable de l'échec.
        @type failure: C{Failure}
        @param idxmpp: Identifiant du message XMPP.
        @type idxmpp: C{str}
        @param payload: Le message reçu à corréler.
        @type payload: C{Element}
        """
        LOGGER.error(_('Unable to store correlated alert for '
                        'message #%(id)s (%(payload)s) : %(error)s'), {
            'id': idxmpp,
            'payload': payload,
            'error': str(failure).decode('utf-8'),
        })

    def connectionInitialized(self):
        """
        Cette méthode est appelée lorsque la connexion avec le bus XMPP
        est prête.
        """
        super(RuleDispatcher, self).connectionInitialized()
        self.rrp.start()

    def connectionLost(self, reason):
        """
        Cette méthode est appelée lorsque la connexion avec le bus XMPP
        est perdue. Dans ce cas, on arrête les tentatives de renvois de
        messages et on arrête le pool de rule runners.
        Le renvoi de messages et le pool seront relancés lorsque la
        connexion sera rétablie (cf. connectionInitialized).
        """
        super(RuleDispatcher, self).connectionLost(reason)
        LOGGER.debug(_('Connection lost, stopping rule runners pool'))
        self.rrp.stop()

    def sendItem(self, item):
        if not isinstance(item, etree.ElementBase):
            item = parseXml(item.encode('utf-8'))
        if item.name == MESSAGEONETOONE:
            self.sendOneToOneXml(item)
            return defer.succeed(None)
        else:
            result = self.publishXml(item)
            return result

    def _do_in_transaction(self, error_desc, xml, ex, func, *args, **kwargs):
        """
        Encapsule une opération nécessitant d'accéder à la base de données
        dans une transaction.

        @param error_desc: Un message d'erreur décrivant la nature de
            l'opération et qui sera affiché si l'opération échoue.
        @type error_desc: C{unicode}
        @param xml: Le message XML sérialisé en cours de traitement.
        @type xml: C{unicode}
        @param ex: Le type d'exceptions à capturer. Il peut également s'agir
            d'une liste de types d'exceptions.
        @type ex: C{Exception} or C{list} of C{Exception}
        @param func: La fonction à appeler pour exécuter l'opération.
        @type func: C{callable}
        @note: Des paramètres additionnels (nommés ou non) peuvent être
            passés à cette fonction. Ils seront transmis tel quel à C{func}
            lors de son appel.
        @post: En cas d'erreur, le message XML est réinséré dans la file
            d'attente du corrélateur pour pouvoir être à nouveau traité
            ultérieurement.
        """
        if not isinstance(ex, list):
            ex = [ex]

        def eb(failure):
            if failure.check(*ex):
                LOGGER.info(_('%s. The message will be handled once more.'),
                    error_desc)
                self.queue.append(xml)
                return failure
            return failure

        d = self._database.run(func, *args, **kwargs)
        d.addErrback(eb)
        return d

    def getStats(self):
        """Récupère des métriques de fonctionnement du corrélateur"""
        def add_exec_stats(stats):
            rule_stats = self._executor.getStats()
            stats.update(rule_stats)
            if self._correl_times:
                stats["rule-total"] = round(sum(self._correl_times) /
                                            len(self._correl_times), 5)
                self._correl_times = []
            return stats
        d = super(RuleDispatcher, self).getStats()
        d.addCallback(add_exec_stats)
        return d

    def registerCallback(self, fn, idnt):
        self.tree_end.addCallback(fn, self, self._database, idnt)
