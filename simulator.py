import torch as th
from util import load_graph, load_graph_auto
from util import convert_matrix_to_vector
from typing import List, Union, Tuple
from util import build_adjacency_matrix_auto, build_adjacency_indies_auto, obtain_num_nodes_auto, GraphList, calc_device
try:
    import matplotlib as mpl
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

TEN = th.Tensor
INT = th.IntTensor

class MaxcutSimulator:  # Markov Chain Monte Carlo Simulator
    def __init__(self, graph_name: str = 'powerlaw_64', gpu_id: int = -1, graph_tuple=None):
        device = calc_device(gpu_id)
        int_type = th.int32
        self.device = device
        self.int_type = int_type

        if graph_tuple:
            graph, num_nodes, num_edges = graph_tuple
        else:
            graph, num_nodes, num_edges = load_graph(graph_name=graph_name)

        # 建立邻接矩阵，不预先保存索引的邻接矩阵不适合GPU并行
        '''
        例如，无向图里：
        - 节点0连接了节点1
        - 节点0连接了节点2
        - 节点2连接了节点3

        用邻接阶矩阵Ary的上三角表示这个无向图：
          0 1 2 3
        0 F T T F
        1 _ F F F
        2 _ _ F T
        3 _ _ _ F

        其中：    
        - Ary[0,1]=True
        - Ary[0,2]=True
        - Ary[2,3]=True
        - 其余为False
        '''
        adjacency_matrix = th.empty((num_nodes, num_nodes), dtype=th.float32, device=device)
        adjacency_matrix[:] = -1  # 选用-1而非0表示表示两个node之间没有edge相连，避免两个节点的距离为0时出现冲突
        for n0, n1, dt in graph:
            adjacency_matrix[n0, n1] = dt
        assert num_nodes == adjacency_matrix.shape[0] == adjacency_matrix.shape[1]
        assert num_edges == (adjacency_matrix != -1).sum()
        self.adjacency_matrix = adjacency_matrix
        self.adjacency_vector = convert_matrix_to_vector(adjacency_matrix)

        # 建立二维列表n0_to_n1s 表示这个图，
        """
        用二维列表list2d表示这个图：
        [
            [1, 2], 
            [], 
            [3],
            [],
        ]
        其中：
        - list2d[0] = [1, 2]
        - list2d[2] = [3]

        对于稀疏的矩阵，可以直接记录每条边两端节点的序号，用shape=(2,N)的二维列表 表示这个图：
        0, 1
        0, 2
        2, 3
        如果条边的长度为1，那么表示为shape=(2,N)的二维列表，并在第一行，写上 4个节点，3条边的信息，帮助重建这个图，然后保存在txt里：
        4, 3
        0, 1, 1
        0, 2, 1
        2, 3, 1
        """
        n0_to_n1s = [[] for _ in range(num_nodes)]  # 将 node0_id 映射到 node1_id
        n0_to_dts = [[] for _ in range(num_nodes)]  # 将 mode0_id 映射到 node1_id 与 node0_id 的距离
        for n0, n1, dist in graph:
            n0_to_n1s[n0].append(n1)
            n0_to_dts[n0].append(dist)
        n0_to_n1s = [th.tensor(node1s, dtype=int_type, device=device) for node1s in n0_to_n1s]
        n0_to_dts = [th.tensor(node1s, dtype=int_type, device=device) for node1s in n0_to_dts]
        assert num_nodes == len(n0_to_n1s)
        assert num_nodes == len(n0_to_dts)
        assert num_edges == sum([len(n0_to_n1) for n0_to_n1 in n0_to_n1s])
        assert num_edges == sum([len(n0_to_dt) for n0_to_dt in n0_to_dts])
        self.num_nodes = len(n0_to_n1s)
        self.num_edges = sum([len(n0_to_n1) for n0_to_n1 in n0_to_n1s])

        # 根据二维列表n0_to_n1s 建立基于edge 的node0 node1 的索引，用于高效计算
        """
        在K个子环境里，需要对N个点进行索引去计算计算GraphMaxCut距离：
        - 建立邻接矩阵的方法，计算GraphMaxCut距离时，需要索引K*N次
        - 下面这种方法直接保存并行索引信息，仅需要索引1次

        为了用GPU加速计算，可以用两个固定长度的张量记录端点序号，再用两个固定长度的张量记录端点信息。去表示这个图：
        我们直接将每条edge两端的端点称为：左端点node0 和 右端点node1 （在无向图里，左右端点可以随意命名）
        node0_id   [0, 0, 2]  # 依次保存三条边的node0，用于索引
        node0_prob [p, p, p]  # 依次根据索引得到node0 的概率，用于计算
        node1_id   [1, 2, 3]  # 依次保存三条边的node1，用于索引
        node1_prob [p, p, p]  # 依次根据索引得到node1 的概率，用于计算

        env_id     [0, 1, 2, ..., num_envs]  # 保存了并行维度的索引信息
        """
        n0_ids = []
        n1_ids = []
        for i, n1s in enumerate(n0_to_n1s):
            n0_ids.extend([i, ] * n1s.shape[0])
            n1_ids.extend(n1s)
        self.n0_ids = th.tensor(n0_ids, dtype=int_type, device=device).unsqueeze(0)
        self.n1_ids = th.tensor(n1_ids, dtype=int_type, device=device).unsqueeze(0)
        self.env_is = th.zeros(self.num_edges, dtype=int_type, device=device).unsqueeze(0)

    def get_objectives_using_for_loop(self, probs: TEN) -> TEN:  # 使用for循环重复查找索引，不适合GPU并行
        assert probs.shape[-1] == self.num_nodes
        num_envs = probs.shape[0]

        sum_dts = []
        for env_i in range(num_envs):  # 逐个访问子环境
            p0 = probs[env_i]

            n0_to_p1 = []
            for n0 in range(self.num_nodes):  # 逐个访问节点
                n1s = th.where(self.adjacency_matrix[n0] != -1)[0]  # 根据邻接矩阵，找出与node0 相连的多个节点的索引
                p1 = p0[n1s]  # 根据索引找出node1 属于集合的概率
                n0_to_p1.append(p1)

            sum_dt = []
            for _p0, _p1 in zip(p0, n0_to_p1):
                # `_p0 * (1-_p1)` node_0 属于这个集合 且 node1 属于那个集合的概率
                # `_p1 * (1-_p0)` node_1 属于这个集合 且 node0 属于那个集合的概率
                # dt = _p0 * (1-_p1) + _p1 * (1-_p0)  # 等价于以下一行代码，相加计算出了这条边两端的节点分别属于两个集合的概率
                dt = _p0 + _p1 - 2 * _p0 * _p1
                # 此计算只能算出的局部梯度，与全局梯度有差别，未考虑无向图里节点间的复杂关系，需要能跳出局部最优的求解器
                sum_dt.append(dt.sum(dim=0))
            sum_dt = th.stack(sum_dt).sum(dim=-1)  # 求和得到这个子环境的 objective
            sum_dts.append(sum_dt)
        sum_dts = th.hstack(sum_dts)  # 堆叠结果，得到 num_envs 个子环境的 objective
        return -sum_dts

    def get_objectives(self, probs: TEN):
        p0s, p1s = self.get_p0s_p1s(probs)
        return -(p0s + p1s - 2 * p0s * p1s).sum(1)

    def get_scores(self, probs: INT) -> INT:
        p0s, p1s = self.get_p0s_p1s(probs)
        return (p0s ^ p1s).sum(1)

    def get_p0s_p1s(self, probs: TEN) -> (TEN, TEN):
        num_envs = probs.shape[0]
        if num_envs != self.env_is.shape[0]:
            self.n0_ids = self.n0_ids[0].repeat(num_envs, 1)
            self.n1_ids = self.n1_ids[0].repeat(num_envs, 1)
            self.env_is = self.env_is[0:1] + th.arange(num_envs, device=self.device).unsqueeze(1)

        p0s = probs[self.env_is, self.n0_ids]
        p1s = probs[self.env_is, self.n1_ids]
        return p0s, p1s

    def get_rand_probs(self, num_envs: int) -> TEN:
        return th.rand((num_envs, self.num_nodes), dtype=th.float32, device=self.device)

    @staticmethod
    def prob_to_bool(p0s, thresh=0.5):
        return p0s > thresh


class MaxcutSimulatorAutoregressive:
    def __init__(self, graph_name: str, device=th.device('cpu'), if_bidirectional: bool = False):
        self.device = device
        self.sim_name = graph_name
        self.int_type = int_type = th.long
        self.if_bidirectional = if_bidirectional

        '''load graph'''
        [graph, self.num_nodes, self.num_edges] = load_graph_auto(graph_name=graph_name)

        '''建立邻接矩阵'''
        self.adjacency_matrix = build_adjacency_matrix_auto(graph=graph, if_bidirectional=if_bidirectional).to(device)

        '''建立邻接索引'''
        n0_to_n1s, n0_to_dts = build_adjacency_indies_auto(graph=graph, if_bidirectional=if_bidirectional)
        n0_to_n1s = [t.to(int_type).to(device) for t in n0_to_n1s]
        # self.num_nodes = obtain_num_nodes_auto(graph)
        # self.num_edges = len(graph)
        self.adjacency_indies = n0_to_n1s

        '''基于邻接索引，建立基于边edge的索引张量：(n0_ids, n1_ids)是所有边(第0个, 第1个)端点的索引'''
        n0_to_n0s = [(th.zeros_like(n1s) + i) for i, n1s in enumerate(n0_to_n1s)]
        self.n0_ids = th.hstack(n0_to_n0s)[None, :]
        self.n1_ids = th.hstack(n0_to_n1s)[None, :]
        len_sim_ids = self.num_edges * (2 if if_bidirectional else 1)
        self.sim_ids = th.zeros(len_sim_ids, dtype=int_type, device=device)[None, :]
        self.n0_num_n1 = th.tensor([n1s.shape[0] for n1s in n0_to_n1s], device=device)[None, :]
    def calculate_obj_values(self, solutions: TEN, if_sum: bool = True) -> TEN:
        num_sims = solutions.shape[0]
        if num_sims != self.sim_ids.shape[0]:
            self.n0_ids = self.n0_ids[0].repeat(num_sims, 1)
            self.n1_ids = self.n1_ids[0].repeat(num_sims, 1)
            self.sim_ids = self.sim_ids[0:1] + th.arange(num_sims, dtype=self.int_type, device=self.device)[:, None]

        values = solutions[self.sim_ids, self.n0_ids] ^ solutions[self.sim_ids, self.n1_ids]
        if if_sum:
            values = values.sum(1)
        if self.if_bidirectional:
            values = values.float() / 2
        return values

    def calculate_obj_values_for_loop(self, xs: TEN, if_sum: bool = True) -> TEN:  # 有更高的并行度，但计算耗时增加一倍。
        num_sims, num_nodes = xs.shape
        values = th.zeros((num_sims, num_nodes), dtype=self.int_type, device=self.device)
        for node0 in range(num_nodes):
            node1s = self.adjacency_indies[node0]
            if node1s.shape[0] > 0:
                values[:, node0] = (xs[:, node0, None] ^ xs[:, node1s]).sum(dim=1)

        if if_sum:
            values = values.sum(dim=1)
        if self.if_bidirectional:
            values = values.float() / 2
        return values

    def generate_solutions_randomly(self, num_sims):
        xs = th.randint(0, 2, size=(num_sims, self.num_nodes), dtype=th.bool, device=self.device)
        xs[:, 0] = 0
        return xs


class MaxcutSimulatorReinforce:
    def __init__(self, graph: GraphList, device=th.device('cpu'), if_bidirectional: bool = False):
        self.device = device
        self.int_type = int_type = th.long
        self.if_bidirectional = if_bidirectional

        '''建立邻接矩阵'''
        self.adjacency_matrix = build_adjacency_matrix_auto(graph=graph, if_bidirectional=if_bidirectional).to(device)

        '''建立邻接索引'''
        n0_to_n1s, n0_to_dts = build_adjacency_indies_auto(graph=graph, if_bidirectional=if_bidirectional)
        n0_to_n1s = [t.to(int_type).to(device) for t in n0_to_n1s]
        self.num_nodes = obtain_num_nodes_auto(graph)
        self.num_edges = len(graph)
        self.adjacency_indies = n0_to_n1s

        '''基于邻接索引，建立基于边edge的索引张量：(n0_ids, n1_ids)是所有边(第0个, 第1个)端点的索引'''
        n0_to_n0s = [(th.zeros_like(n1s) + i) for i, n1s in enumerate(n0_to_n1s)]
        self.n0_ids = th.hstack(n0_to_n0s)[None, :]
        self.n1_ids = th.hstack(n0_to_n1s)[None, :]
        len_sim_ids = self.num_edges * (2 if if_bidirectional else 1)
        self.sim_ids = th.zeros(len_sim_ids, dtype=int_type, device=device)[None, :]
        self.n0_num_n1 = th.tensor([n1s.shape[0] for n1s in n0_to_n1s], device=device)[None, :]

    def calculate_obj_values(self, xs: TEN, if_sum: bool = True) -> TEN:
        num_sims = xs.shape[0]
        if num_sims != self.sim_ids.shape[0]:
            self.n0_ids = self.n0_ids[0].repeat(num_sims, 1)
            self.n1_ids = self.n1_ids[0].repeat(num_sims, 1)
            self.sim_ids = self.sim_ids[0:1] + th.arange(num_sims, dtype=self.int_type, device=self.device)[:, None]

        values = xs[self.sim_ids, self.n0_ids] ^ xs[self.sim_ids, self.n1_ids]
        if if_sum:
            values = values.sum(1)
        if self.if_bidirectional:
            values = values.float() / 2
        return values

    def calculate_obj_values_for_loop(self, xs: TEN, if_sum: bool = True) -> TEN:  # 有更高的并行度，但计算耗时增加一倍。
        num_sims, num_nodes = xs.shape
        values = th.zeros((num_sims, num_nodes), dtype=self.int_type, device=self.device)
        for node0 in range(num_nodes):
            node1s = self.adjacency_indies[node0]
            if node1s.shape[0] > 0:
                values[:, node0] = (xs[:, node0, None] ^ xs[:, node1s]).sum(dim=1)

        if if_sum:
            values = values.sum(dim=1)
        if self.if_bidirectional:
            values = values.float() / 2
        return values

    def generate_solutions_randomly(self, num_sims):
        solutions = th.randint(0, 2, size=(num_sims, self.num_nodes), dtype=th.bool, device=self.device)
        solutions[:, 0] = 0
        return solutions