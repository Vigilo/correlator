# vim: set fileencoding=utf-8 sw=4 ts=4 et :
# pylint: disable-msg=C0111,W0212,R0904
# Copyright (C) 2006-2011 CS-SI
# License: GNU GPL v2 <http://www.gnu.org/licenses/gpl-2.0.html>

"""
Teste la désaggrégation d'un événement corrélé (#467).
"""

import time
from datetime import datetime
import unittest

from nose.twistedtools import reactor, deferred
from twisted.internet import defer
from lxml import etree

from mock import Mock
import helpers
from vigilo.correlator.test.helpers import ContextStubFactory, \
                                            RuleDispatcherStub

from vigilo.pubsub.xml import NS_EVENT
from vigilo.models.session import DBSession
from vigilo.models import tables
from vigilo.correlator.correvent import make_correvent
from vigilo.correlator.db_thread import DummyDatabaseWrapper

from vigilo.common.logging import get_logger
LOGGER = get_logger(__name__)

class TestCorrevents4(unittest.TestCase):
    @deferred(timeout=30)
    def setUp(self):
        """Initialise la BDD au début de chaque test."""
        super(TestCorrevents4, self).setUp()
        helpers.setup_db()
        helpers.populate_statename()
        self.forwarder = RuleDispatcherStub()
        self.context_factory = ContextStubFactory()
        self.make_deps()
        self.ts = int(time.time()) - 10
        return defer.succeed(None)

    @deferred(timeout=30)
    def tearDown(self):
        """Nettoie la BDD à la fin de chaque test."""
        super(TestCorrevents4, self).tearDown()
        helpers.teardown_db()
        self.context_factory.reset()
        return defer.succeed(None)

    def make_deps(self):
        """
        Création de 4 hôtes "Host 1" jusqu'à "Host 4".
        """
        self.hosts = {}
        for i in xrange(1, 4 + 1):
            self.hosts[i] = tables.Host(
                name = u'Host %d' % i,
                snmpcommunity = u'com11',
                hosttpl = u'tpl11',
                address = u'192.168.0.11',
                snmpport = 11,
                weight = 42,
            )
            DBSession.add(self.hosts[i])
            DBSession.flush()
            print "Added %s with ID #%d" % (
                self.hosts[i].name,
                self.hosts[i].idhost)
        print ""

    @defer.inlineCallbacks
    def handle_alert(self, host, new_state, preds=None, succs=None):
        """
        Simule l'arrivée d'une alerte concernant un hôte
        avec l'état donné en argument.
        """
        new_state = unicode(new_state)

        if preds is None:
            preds = []
        if succs is None:
            succs = []

        self.ts += 1
        info_dictionary = {
            'timestamp': self.ts,
            'host': host.name,
            'service': u'',
            'state': new_state,
            'message': new_state,
            'xmlns': NS_EVENT,
        }

        ctx = self.context_factory(self.ts)

        payload = """
<event xmlns="%(xmlns)s">
    <timestamp>%(timestamp)s</timestamp>
    <host>%(host)s</host>
    <service>%(service)s</service>
    <state>%(state)s</state>
    <message>%(state)s</message>
</event>
""" % info_dictionary
        item = etree.fromstring(payload)
        # À présent, le timestamp doit être un objet datetime.
        # On fait la conversion directement ici.
        info_dictionary['timestamp'] = datetime.fromtimestamp(self.ts)

        # Création Event.
        event = DBSession.query(tables.Event).filter(
            tables.Event.idsupitem == host.idhost).first()
        if event is None:
            event = tables.Event(idsupitem=host.idhost)

        event.current_state = tables.StateName.statename_to_value(
                                info_dictionary['state'])
        event.message = unicode(info_dictionary['message'])
        event.timestamp = info_dictionary['timestamp']
        DBSession.add(event)
        DBSession.flush()

        open_aggr = DBSession.query(
                tables.CorrEvent.idcorrevent
            ).filter(tables.CorrEvent.idcause == event.idevent
            ).scalar()
        # open_aggr vaut None si aucun événement corrélé
        # n'exite pour le moment pour l'élément.
        # Le contexte doit contenir 0 à la place pour ce cas.
        open_aggr = open_aggr or 0

        # On passe par une DeferredList pour garantir l'exécution
        # de tous les Deferred comme étant un seul bloc logique.
        yield defer.DeferredList([
            ctx.set('hostname', host.name),
            ctx.set('servicename', ''),
            ctx.set('statename', new_state),
            ctx.set('raw_event_id', event.idevent),
            ctx.set('idsupitem', host.idhost),
            ctx.set('payload', payload),
            ctx.set('timestamp', info_dictionary['timestamp']),
            ctx.set('predecessors_aggregates', preds),
            ctx.set('successors_aggregates', succs),
            ctx.setShared('open_aggr:%s' % host.idhost, open_aggr),
        ])

        res = yield make_correvent(
            self.forwarder,
            DummyDatabaseWrapper(True),
            item,
            self.ts,
            info_dictionary,
            self.context_factory,
        )
        DBSession.flush()

        idcorrevent = DBSession.query(
                tables.CorrEvent.idcorrevent
            ).filter(tables.CorrEvent.idcause == event.idevent
            ).scalar()
        # Le expunge_all() évite que SQLAlchemy ne garde en cache
        # la valeur du .events des CorrEvents.
        DBSession.expunge_all()
        defer.returnValue( (res, idcorrevent) )

    @deferred(timeout=30)
    @defer.inlineCallbacks
    def test_desaggregate(self):
        """Désagrégation des événements corrélés (#467)."""
        # Ajout des dépendances topologiques :
        # - Host 2 dépend de Host 1
        # - Host 4 dépend de Host 1
        # - Host 3 dépend de Host 3
        dep_group = tables.DependencyGroup(
            dependent=self.hosts[2],
            role=u'topology',
            operator=u'|',
        )
        DBSession.add(dep_group)
        DBSession.add(tables.Dependency(
            group=dep_group,
            supitem=self.hosts[1],
            distance=1,
        ))

        dep_group = tables.DependencyGroup(
            dependent=self.hosts[4],
            role=u'topology',
            operator=u'|',
        )
        DBSession.add(dep_group)
        DBSession.add(tables.Dependency(
            group=dep_group,
            supitem=self.hosts[1],
            distance=1,
        ))

        dep_group = tables.DependencyGroup(
            dependent=self.hosts[3],
            role=u'topology',
            operator=u'|',
        )
        DBSession.add(dep_group)
        DBSession.add(tables.Dependency(
            group=dep_group,
            supitem=self.hosts[4],
            distance=1,
        ))
        DBSession.add(tables.Dependency(
            group=dep_group,
            supitem=self.hosts[1],
            distance=2,
        ))
        DBSession.flush()

        # 1. Un 1er agrégat doit avoir été créé.
        res, idcorrevent1 = yield self.handle_alert(self.hosts[2], 'UNREACHABLE')
        print "Finished step 1\n"
        # Aucune erreur n'a été levée.
        self.assertNotEquals(res, None)
        self.assertNotEquals(idcorrevent1, None)
        # Un agrégat a été créé sur cet hôte...
        db_correvent = DBSession.query(tables.CorrEvent).get(idcorrevent1)
        self.assertEquals(self.hosts[2].idhost, db_correvent.cause.idsupitem)
        # ... dans l'état indiqué.
        self.assertEquals(
            u'UNREACHABLE',
            tables.StateName.value_to_statename(
                db_correvent.cause.current_state)
        )
        # ... contenant uniquement un événement (cause hôte 2).
        self.assertEquals(
            [u'Host 2'],
            [ev.supitem.name for ev in db_correvent.events]
        )

        # 2. Un nouvel agrégat doit avoir été créé et l'agrégat
        #    précédent doit avoir été fusionné dans celui-ci.
        res, idcorrevent2 = yield self.handle_alert(
            self.hosts[1], 'DOWN', succs=[idcorrevent1])
        print "Finished step 2\n"
        # Aucune erreur n'a été levée.
        self.assertNotEquals(res, None)
        self.assertNotEquals(idcorrevent2, None)
        # Il ne doit rester qu'un seul agrégat (le 1er a été fusionné).
        db_correvent = DBSession.query(tables.CorrEvent).one()
        self.assertEquals(db_correvent.idcorrevent, idcorrevent2)
        # ... dont la cause est l'hôte 1.
        self.assertEquals(self.hosts[1].idhost, db_correvent.cause.idsupitem)
        # ... dans l'état indiqué.
        self.assertEquals(
            u'DOWN',
            tables.StateName.value_to_statename(
                db_correvent.cause.current_state)
        )
        # ... ayant 2 événements bruts rattachés (cause hôte 1 + hôte 2).
        self.assertEquals(
            [u'Host 1', u'Host 2'],
            sorted([ev.supitem.name for ev in db_correvent.events])
        )

        # 3. Pas de nouvel agrégat, mais un nouvel événement brut (hôte 4)
        #    ajouté à l'agrégat de l'étape 2.
        res, idcorrevent3 = yield self.handle_alert(
            self.hosts[4], 'UNREACHABLE', preds=[idcorrevent2])
        print "Finished step 3\n"
        # Aucune erreur n'a été levée.
        self.assertEquals(res, None)
        self.assertEquals(idcorrevent3, None) # ajouté dans l'agrégat 2.
        # Toujours un seul agrégat.
        db_correvent = DBSession.query(tables.CorrEvent).one()
        self.assertEquals(db_correvent.idcorrevent, idcorrevent2)
        # ... dont la cause est l'hôte 1.
        self.assertEquals(self.hosts[1].idhost, db_correvent.cause.idsupitem)
        # ... dans l'état indiqué.
        self.assertEquals(
            u'DOWN',
            tables.StateName.value_to_statename(
                db_correvent.cause.current_state)
        )
        # ... ayant 3 événements bruts.
        self.assertEquals(
            [u'Host 1', u'Host 2', u'Host 4'],
            sorted([ev.supitem.name for ev in db_correvent.events])
        )

        # 4. Pas de nouvel agrégat, mais un nouvel événement brut (hôte 3)
        #    ajouté à l'agrégat de l'étape 2.
        res, idcorrevent4 = yield self.handle_alert(
            self.hosts[3], 'UNREACHABLE', preds=[idcorrevent2])
        print "Finished step 4\n"
        # Aucune erreur n'a été levée.
        self.assertEquals(res, None)
        self.assertEquals(idcorrevent4, None) # ajouté dans l'agrégat 2.
        # On a 4 événements bruts en base.
        self.assertEquals(4, DBSession.query(tables.Event).count())
        # On a toujours un seul agrégat.
        db_correvent = DBSession.query(tables.CorrEvent).one()
        self.assertEquals(db_correvent.idcorrevent, idcorrevent2)
        # ... dont la cause est l'hôte 1.
        self.assertEquals(self.hosts[1].idhost, db_correvent.cause.idsupitem)
        # ... dans l'état indiqué.
        self.assertEquals(
            u'DOWN',
            tables.StateName.value_to_statename(
                db_correvent.cause.current_state)
        )
        # ... ayant 3 événements bruts.
        self.assertEquals(
            [u'Host 1', u'Host 2', u'Host 3', u'Host 4'],
            sorted([ev.supitem.name for ev in db_correvent.events])
        )

        # 5. L'agrégat de l'étape 2 doit avoir été désagrégé
        #    en 3 agrégats, l'un signalant que l'hôte 1 est UP,
        #    un autre indiquant que l'hôte 2 est UNREACHABLE,
        #    le dernier donnant les hôtes 4 et 3 UNREACHABLE.
        res, idcorrevent5 = yield self.handle_alert(self.hosts[1], 'UP')
        print "Finished step 5\n"
        # Aucune erreur n'a été levée.
        self.assertNotEquals(res, None)
        # Désagrégé à partir de l'agrégat 2.
        self.assertEquals(idcorrevent5, idcorrevent2)
        # On a 4 événements bruts et 3 agrégats en base.
        print "events"
        self.assertEquals(4, DBSession.query(tables.Event).count())
        db_correvents = DBSession.query(tables.CorrEvent).all()
        print "correvents"
        self.assertEquals(3, len(db_correvents))
        db_correvents.sort(key=lambda x: x.cause.supitem.name)
        # L'un porte sur l'hôte 1 qui doit être dans l'état "UP"
        # et ne contient qu'un seul événement brut sur host 1.
        self.assertEquals(self.hosts[1].idhost, db_correvents[0].cause.idsupitem)
        self.assertEquals(
            u'UP',
            tables.StateName.value_to_statename(
                db_correvents[0].cause.current_state)
        )
        self.assertEquals(
            [u'Host 1'],
            sorted([ev.supitem.name for ev in db_correvents[0].events])
        )
        # Le second porte sur l'hôte 2, qui se trouve toujours dans
        # l'état "UNREACHABLE" et n'a qu'un seul événement brut (host 2).
        self.assertEquals(self.hosts[2].idhost, db_correvents[1].cause.idsupitem)
        self.assertEquals(
            u'UNREACHABLE',
            tables.StateName.value_to_statename(
                db_correvents[1].cause.current_state)
        )
        self.assertEquals(
            [u'Host 2'],
            sorted([ev.supitem.name for ev in db_correvents[1].events])
        )
        # Le dernier des agrégats porte sur l'hôte 4
        # qui se trouve dans l'état UNREACHABLE et
        # contient 2 événements bruts (host 4 et host 3).
        self.assertEquals(self.hosts[4].idhost, db_correvents[2].cause.idsupitem)
        self.assertEquals(
            u'UNREACHABLE',
            tables.StateName.value_to_statename(
                db_correvents[2].cause.current_state)
        )
        self.assertEquals(
            [u'Host 3', u'Host 4'],
            sorted([ev.supitem.name for ev in db_correvents[2].events])
        )


    @deferred(timeout=30)
    @defer.inlineCallbacks
    def test_pseudo_triangle(self):
        """
        Désagrégation avec événements en pseudo-triangle.

        Lorsque 2 hôtes tombent et qu'un 3ème hôte dépendant des 2 premiers
        au sens de la topologie devient indisponible, l'événement brut sur
        ce 3ème hôte doit être agrégé dans les agrégats des 2 premiers.

        Lorsque le 1er hôte redevient opérationnel,
        """
        # Ajout des dépendances topologiques :
        # - Host 3 dépend de Host 1 et Host 2 (triangle).
        dep_group = tables.DependencyGroup(
            dependent=self.hosts[3],
            role=u'topology',
            operator=u'|',
        )
        DBSession.add(dep_group)
        DBSession.add(tables.Dependency(
            group=dep_group,
            supitem=self.hosts[1],
            distance=1,
        ))
        DBSession.add(tables.Dependency(
            group=dep_group,
            supitem=self.hosts[2],
            distance=1,
        ))
        DBSession.flush()

        # Simule la chute des hôtes "Host 1" et "Host 2", puis l'indisponibilité
        # de l'hôte "Host 3" qui dépend des 2 autres topologiquement.
        res, idcorrevent1 = yield self.handle_alert(self.hosts[1], 'DOWN')
        self.assertNotEquals(res, None)
        res, idcorrevent2 = yield self.handle_alert(self.hosts[2], 'DOWN')
        self.assertNotEquals(res, None)
        res, idcorrevent3 = yield self.handle_alert(
            self.hosts[3],
            'UNREACHABLE',
            preds=[idcorrevent1, idcorrevent2],
        )
        self.assertEquals(res, None) # Pas de nouvel agrégat créé.
        print "Finished step 1\n"
        self.assertNotEquals(idcorrevent1, None)
        self.assertNotEquals(idcorrevent2, None)
        self.assertEquals(idcorrevent3, None)
        # On s'attend à trouver 3 événements bruts et 2 agrégats.
        self.assertEquals(3, DBSession.query(tables.Event).count())
        self.assertEquals(2, DBSession.query(tables.CorrEvent).count())
        # L'événement brut sur "Host 3" a été agrégé dans les 2 autres.
        event3 = DBSession.query(tables.Event).filter(
            tables.Event.idsupitem == self.hosts[3].idhost).one()
        # ... donc il appartient à l'agrégat de l'hôte 1.
        correvent = DBSession.query(tables.CorrEvent).get(idcorrevent1)
        self.assertTrue(event3 in correvent.events)
        # ... ainsi qu'à celui de l'hôte 2.
        correvent = DBSession.query(tables.CorrEvent).get(idcorrevent2)
        self.assertTrue(event3 in correvent.events)

        # Simule la remontée de "Host 1" : l'événement brut concernant "Host 3"
        # doit être retiré des agrégats de l'hôte 1 et de l'hôte 2.
        # Un nouvel agrégat doit avoir été créé pour l'accueillir.
        res, idcorrevent = yield self.handle_alert(self.hosts[1], 'UP')
        print "Finished step 2\n"
        self.assertNotEquals(res, None)
        # On s'attend à trouver 3 événements bruts et 3 agrégats.
        self.assertEquals(3, DBSession.query(tables.Event).count())
        self.assertEquals(3, DBSession.query(tables.CorrEvent).count())
        # L'événement brut sur "Host 3" n'est plus l'agrégat de l'hôte 1.
        correvent = DBSession.query(tables.CorrEvent).get(idcorrevent1)
        self.assertFalse(event3 in correvent.events)
        # ... ni dans celui de l'hôte 2.
        correvent = DBSession.query(tables.CorrEvent).get(idcorrevent2)
        self.assertFalse(event3 in correvent.events)
        # ... en revanche, il dispose de son propre agrégat.
        correvent = DBSession.query(
                tables.CorrEvent
            ).join(
                (tables.Event, tables.Event.idevent ==
                    tables.CorrEvent.idcause),
            ).filter(tables.Event.idsupitem == self.hosts[3].idhost
            ).one()

        # "Host 2" remonte : rien ne change.
        res, idcorrevent = yield self.handle_alert(self.hosts[2], 'UP')
        print "Finished step 3\n"
        self.assertNotEquals(res, None)
        # On s'attend à trouver 3 événements bruts et 3 agrégats.
        self.assertEquals(3, DBSession.query(tables.Event).count())
        self.assertEquals(3, DBSession.query(tables.CorrEvent).count())
        # L'événement brut sur "Host 3" n'est plus l'agrégat de l'hôte 1.
        correvent = DBSession.query(tables.CorrEvent).get(idcorrevent1)
        self.assertFalse(event3 in correvent.events)
        # ... ni dans celui de l'hôte 2.
        correvent = DBSession.query(tables.CorrEvent).get(idcorrevent2)
        self.assertFalse(event3 in correvent.events)
        # ... en revanche, il dispose de son propre agrégat.
        correvent = DBSession.query(
                tables.CorrEvent
            ).join(
                (tables.Event, tables.Event.idevent ==
                    tables.CorrEvent.idcause),
            ).filter(tables.Event.idsupitem == self.hosts[3].idhost
            ).one()
