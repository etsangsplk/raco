import unittest
from raco.language.join_graph import JoinGraph


class JoinGraphTest(unittest.TestCase):
    def test_no_cross_product(self):
        jg = JoinGraph(3)
        jg.add_edge(0, 2, "hello")
        jg.add_edge(2, 0, "world")
        jg.add_edge(2, 1, "goodbye")

        jo = jg.choose_left_deep_join_order()
        self.assertIn(tuple(jo), {(0,2,1), (2,1,0), (2,0,1), (1,2,0)})

        self.assertEquals(jg.get_edges(0, 2), {"hello", "world"})
        self.assertEquals(jg.get_edges(2, 0), {"hello", "world"})
        self.assertEquals(jg.get_edges(1, 2), {"goodbye"})
        self.assertEquals(jg.get_edges(2, 1), {"goodbye"})
        self.assertEquals(jg.get_edges(0, 1), set())
        self.assertEquals(jg.get_edges(1, 0), set())