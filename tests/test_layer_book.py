import unittest
from itertools import permutations

from trading_core.layers import Action, Enter, Fill, LayerBook, Protect, Terminal, Unknown, Working


class LayerBookTests(unittest.TestCase):
    def seed(self, book, lots=1, direction=1):
        self.assertEqual(book.apply(Enter('o1', direction, lots)), (Action('open', 'o1', 'L1', lots),))
        self.assertEqual(book.layers, ())
        self.assertEqual(book.apply(Protect('premature')), ())
        book.apply(Fill('o1', 'f1', lots))
        self.assertEqual(book.apply(Protect('no-terminal')), ())
        book.apply(Terminal('o1', lots))
        self.assertEqual(book.apply(Protect('p1')), (Action('take_profit', 'p1', 'L1', lots),))
        book.apply(Working('p1'))

    def test_three_layers_round_trip_both_directions(self):
        for d in (-1, 1):
            book = LayerBook()
            self.seed(book, direction=d)
            for i in (2, 3):
                o, p, tier = f'o{i}', f'p{i}', f'L{i}'
                old, old_tier = f'p{i-1}', f'L{i-1}'
                self.assertEqual(book.apply(Enter(o, d)), (Action('add', o, tier, 1),))
                self.assertEqual(book.worst_case_lots, i)
                self.assertEqual(book.apply(Enter('extra' + o, d)), ())
                self.assertEqual(book.apply(Fill(o, 'f' + o, 1)), (Action('cancel', old, old_tier, 1),))
                book.apply(Terminal(o, 1))
                self.assertEqual(book.apply(Protect('waiting' + p)), ())
                self.assertEqual(book.apply(Enter('waiting' + o, d)), ())
                book.apply(Terminal(old, 0))
                self.assertEqual(book.apply(Protect(p)), (Action('take_profit', p, tier, 1),))
                book.apply(Working(p))
            self.assertEqual(book.apply(Enter('full', d)), ())
            p = 'p3'
            for i in (3, 2, 1):
                self.assertEqual(book.layers[-1].tier_id, f'L{i}')
                book.apply(Fill(p, 'f' + p, 1))
                self.assertEqual(book.apply(Protect('early' + p)), ())
                book.apply(Terminal(p, 1))
                p = f'restored{i-1}'
                want = (Action('take_profit', p, f'L{i-1}', 1),) if i > 1 else ()
                self.assertEqual(book.apply(Protect(p)), want)
            self.assertIsNone(book.direction)
            self.assertEqual(book.apply(Enter('reverse', -d)), (Action('open', 'reverse', 'L4', 1),))

    def test_two_filled_orders_all_delivery_orders_and_duplicates(self):
        # Neither terminal alone releases a reservation. Old/new can each deliver
        # either the terminal or the fill first, independent of the other order.
        events = (Fill('p1', 'close-old', 1), Terminal('p1', 1),
                  Fill('o2', 'open-new', 1), Terminal('o2', 1))
        for sequence in permutations(events):
            with self.subTest(sequence=sequence):
                book = LayerBook()
                self.seed(book)
                book.apply(Enter('o2', 1))
                cancels = []
                for index, event in enumerate(sequence):
                    cancels.extend(book.apply(event))
                    self.assertEqual(book.apply(event), ())
                    self.assertEqual(book.apply(Enter(f'reverse{index}', -1)), ())
                    self.assertEqual(book.direction, 1)
                    if index < 3:
                        self.assertEqual(book.apply(Protect(f'early{index}')), ())
                self.assertLessEqual(len(cancels), 1)
                self.assertTrue(all(a.kind == 'cancel' for a in cancels))
                if sequence[0] == events[0]:
                    self.assertEqual(cancels, [Action('cancel', 'o2', 'L2', 1)])
                if sequence[0] == events[2]:
                    self.assertEqual(cancels, [Action('cancel', 'p1', 'L1', 1)])
                self.assertEqual([(x.tier_id, x.remaining) for x in book.layers], [('L2', 1)])
                self.assertEqual(book.apply(Protect('p2')), (Action('take_profit', 'p2', 'L2', 1),))

    def test_partial_add_late_detail_and_partial_close(self):
        book = LayerBook(max_lots=4)
        self.seed(book)
        self.assertEqual(book.apply(Enter('oversized', 1, 4)), ())
        book.apply(Enter('o2', 1, 3))
        self.assertEqual(book.apply(Fill('o2', 'fa', 1)),
                         (Action('cancel', 'p1', 'L1', 1), Action('cancel', 'o2', 'L2', 2)))
        self.assertEqual(book.worst_case_lots, 4)
        book.apply(Terminal('p1', 0))
        book.apply(Terminal('o2', 2))
        self.assertEqual(book.apply(Protect('missing-detail')), ())
        book.apply(Fill('o2', 'fb', 1))
        self.assertEqual(book.apply(Protect('p2')), (Action('take_profit', 'p2', 'L2', 2),))
        book.apply(Fill('p2', 'fc', 1))
        self.assertEqual([(x.opened, x.remaining) for x in book.layers], [(1, 1), (2, 1)])
        self.assertEqual(book.apply(Protect('duplicate')), ())
        book.apply(Terminal('p2', 1))
        self.assertEqual(book.apply(Protect('replace')), (Action('take_profit', 'replace', 'L2', 1),))

    def test_unknown_no_blind_retry_zero_fill_releases_only_add(self):
        book = LayerBook()
        self.seed(book)
        book.apply(Unknown('p1'))
        self.assertEqual(book.apply(Enter('unsafe', 1)), ())
        book.apply(Working('p1'))
        book.apply(Enter('o2', 1))
        book.apply(Unknown('o2'))
        self.assertEqual(book.apply(Enter('retry', 1)), ())
        self.assertEqual(book.apply(Protect('retry-tp')), ())
        book.apply(Terminal('o2', 0))
        book.apply(Working('o2'))  # stale active cannot revive terminal
        self.assertEqual(book.apply(Enter('o2', 1)), ())
        self.assertEqual(book.apply(Protect('still-working')), ())
        self.assertEqual(book.apply(Enter('o3', 1)), (Action('add', 'o3', 'L3', 1),))

    def test_replay_and_invalid_facts_do_not_change_book(self):
        events = (Enter('a', -1, 2), Terminal('a', 1), Fill('a', 'key', 1), Protect('b'))
        def replay():
            book = LayerBook()
            actions = tuple(action for event in events for action in book.apply(event))
            for event in events:
                self.assertEqual(book.apply(event), ())
            for bad in (Fill('b', 'key', 1), Terminal('a', 0), Enter('a', 1),
                        Fill('a', 'late-overfill', 1), Fill('unknown', 'x', 1)):
                with self.assertRaises(ValueError):
                    book.apply(bad)
            return actions, book.layers, book.worst_case_lots
        self.assertEqual(replay(), replay())
