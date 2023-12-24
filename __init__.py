import functools
import os
import threading
import time
from collections import defaultdict
from concurrent.futures.thread import ThreadPoolExecutor
from functools import wraps, reduce
from typing import Tuple, Union, Callable, Optional

import pytest
from _pytest.fixtures import FixtureDef, FixtureLookupError, PseudoFixtureDef, SubRequest, scopes, FixtureRequest
from _pytest.nodes import Item
from _pytest.python import Function
from _pytest.runner import SetupState, _update_current_test_var

from loguru import logger

logger.remove(handler_id=None)
logger.add("dispatch-case.log")

# 指定case分组的单元的mark标签字符
CASE_GROUP_UNIT_TAG = "group-unit"
# 指定case分组的标签字符
CASE_GROUP_TAG = "group"
# 启动的线程数量
THREAD_COUNT = "thread"
# 不接受并发
NOTCONCURRENT = "notconcurrent"
# 资源占用互斥marker
RESOURCE = "resource"


def pytest_addoption(parser):
    thread_help = "线程数量，每个线程都会用于启动一个分组，默认值：1"

    # pytest -h 中添加命令帮助信息
    group = parser.getgroup('pytest-groups')
    group.addoption(f"--{THREAD_COUNT}", action="store", default=1, help=thread_help)

    # 添加参数到pytest.ini的配置对象中，效果相当于pytest.ini进行了相关配置
    parser.addini(THREAD_COUNT, type="args", default=1, help=thread_help)

    group_unit_help = "自动分组单元单位，同一个单元单位内的case自动被规划到一个分组内，可选值：module(默认值)/class/function"

    # pytest -h 中添加命令帮助信息
    group = parser.getgroup('pytest-groups')
    group.addoption(f"--{CASE_GROUP_UNIT_TAG}", action="store", default="module", help=group_unit_help)

    # 添加参数到pytest.ini的配置对象中，效果相当于pytest.ini进行了相关配置
    # parser.addini('group-unit', type="args", default="class", help=group_unit_help)
    parser.addini(CASE_GROUP_UNIT_TAG, type="args", default="module", help=group_unit_help)
    # parser.addini('group-unit', type="args", default="function", help=group_unit_help)


def pytest_configure(config):
    thread_count = parse_config(config, THREAD_COUNT)
    # 如果有配置插件相关参数（thread等），才启用插件,默认启用
    if not config.option.collectonly and thread_count:
        config.pluginmanager.register(GroupRunner(config), CASE_GROUP_TAG)


class ThreadLocalSetupState(SetupState, threading.local):
    def __init__(self):
        super(ThreadLocalSetupState, self).__init__()


class ThreadLocalFixtureDef(FixtureDef, threading.local):
    def __init__(self, *args, **kwargs):
        super(ThreadLocalFixtureDef, self).__init__(*args, **kwargs)


#
# setup_fixtrue_map = {}
#
# old_schedule_finalizers = _schedule_finalizers
#
#
# def new_schedule_finalizers(self: FixtureRequest, finalizer: Callable[[], object], scope) -> None:
#     map = setup_fixtrue_map.setdefault(self,{})
#     map[]


class ThreadLocalEnviron(os._Environ):
    def __init__(self, env):
        super().__init__(
            env._data,
            env.encodekey,
            env.decodekey,
            env.encodevalue,
            env.decodevalue,
            env.putenv,
            env.unsetenv
        )
        if hasattr(env, 'thread_store'):
            self.thread_store = env.thread_store
        else:
            self.thread_store = threading.local()

    def __getitem__(self, key):
        if key == 'PYTEST_CURRENT_TEST':
            if hasattr(self.thread_store, key):
                value = getattr(self.thread_store, key)
                return self.decodevalue(value)
            else:
                raise KeyError(key) from None
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key == 'PYTEST_CURRENT_TEST':
            value = self.encodevalue(value)
            self.putenv(self.encodekey(key), value)
            setattr(self.thread_store, key, value)
        else:
            super().__setitem__(key, value)

    def __delitem__(self, key):
        if key == 'PYTEST_CURRENT_TEST':
            self.unsetenv(self.encodekey(key))
            if hasattr(self.thread_store, key):
                delattr(self.thread_store, key)
            else:
                raise KeyError(key) from None
        else:
            super().__delitem__(key)

    def __iter__(self):
        if hasattr(self.thread_store, 'PYTEST_CURRENT_TEST'):
            yield 'PYTEST_CURRENT_TEST'
        keys = list(self._data)
        for key in keys:
            yield self.decodekey(key)

    def __len__(self):
        return len(self.thread_store.__dict__) + len(self._data)

    def copy(self):
        return type(self)(self)


def parse_config(config, name):
    """
    依次尝试从命令参数、pytest.ini配置文件中取得应该生效的参数。

    :param config: 配置对象
    :param name: 配置name
    :return: 生效的配置参数
    """
    t1 = getattr(config.option, name, None)
    if t1:
        return t1

    t2 = config.getoption(f'--{name}')
    if t2:
        return t2

    t3 = config.getini(name)
    if t3:
        return t3[0]
    return None


def has_stack_level_change(item, nextitem):
    """
    检查两个入参的堆栈层级是否有变化
    例如：
    has_stack_level_change("/a/b","/a/c") -> 1,2，0,"/a",False
    has_stack_level_change("/a/b","/a/c/d") -> 1,2，1,"/a/c",True
    has_stack_level_change("/a/b/c","/a/b/d") -> 2,3，0,"/a/b",Fasle
    has_stack_level_change("/a/b/c","/a/d") -> 1,3，-1,"/a",True
    has_stack_level_change("/a/b/c","/a/d/e") -> 1,3,0,"/a/d",True
    :param item: 当前任务
    :param nextitem: 下一个任务
    :return: tuple(共同前缀层数，item任务层数，item层数-next任务层数，共同前缀，是否发生fixture作用域变化)
    """
    if item is None:
        item_collectors = []
    else:
        item_collectors = item.listchain()
    if nextitem is None:
        needed_collectors = []
    else:
        needed_collectors = nextitem.listchain()

    item_layer = len(item_collectors)
    diff_layer = item_layer - len(needed_collectors)

    prefix = 0
    while item_collectors[:prefix] == needed_collectors[:prefix]:
        prefix += 1
    prefix += -1

    if item_collectors == [] or needed_collectors == []:
        prefix_str = ""
    else:
        iter = item_collectors[:prefix]
        iter = map(lambda x: x.name, iter)
        prefix_str = reduce(lambda o, n: o + f",{n}", iter)

    return prefix, item_layer, diff_layer, prefix_str, (diff_layer != 0 or prefix + 1 != item_layer)


class GroupRunner(object):
    def __init__(self, config):
        # 获取应该启动的线程数
        self.thread_count = int(parse_config(config, THREAD_COUNT))
        # case的分组结果
        self.item_dict = {}
        self.item_map_exist = {}
        self.lock = threading.Lock()
        self.tasks = []
        self.task_order = []
        self.task_index = 0
        self.is_notconcurrent = {}
        # 作用域包含的case的集合，当集合为空时，说明作用域已经完全执行完了可以卸载作用域了。
        self.stack_map_case = {}
        # 存储作用域对应的fuxture执行结果
        self.stack_map_fuxturedef = {}

    def pytest_configure(self, config):
        # 声明@pytest.mark.group
        config.addinivalue_line("markers", f"{CASE_GROUP_TAG}: 装饰case(类、模块、函数)，声明case默认默认的分组规则")
        # 声明@pytest.mark.notconcurrent
        config.addinivalue_line("markers", f"{NOTCONCURRENT}: 声明case不接受并发")
        # 声明@pytest.mark.resource
        config.addinivalue_line("markers", f"{RESOURCE}: 声明case使用的资源")

    @pytest.mark.tryfirst
    def pytest_sessionstart(self, session):
        import _pytest
        # 创建线程安全的session
        _pytest.runner.SetupState = ThreadLocalSetupState

        # 确保fixture(特别是终结器)是线程安全的
        # 但是添加这个这个配置之后，会有fixture重入问题
        _pytest.fixtures.FixtureDef = ThreadLocalFixtureDef

        # 创建线程安全的os.environ
        os.environ = ThreadLocalEnviron(os.environ)

        # # FixtureRequest是一个内部类，它用于表示一个测试请求的上下文。这个类提供了对测试用例执行过程中的各种信息和状态的访问。
        # # 当你在测试用例或fixture中使用request对象时，你实际上是在与FixtureRequest实例进行交互。
        # # 可以认为存在一个名为request的fixture,
        # # request对象可以：
        # # 1. 访问测试上下文：它允许你访问当前测试的配置、参数、所属模块、类、实例等信息。
        # # 2. 参数化支持：如果fixture被参数化，FixtureRequest对象将包含一个param属性，允许你访问当前测试用例的参数值。
        # # 3. 添加finalizer：你可以使用addfinalizer方法为测试添加清理函数，这些函数会在测试用例执行完成后调用。
        # # 4. 动态获取fixture：通过getfixturevalue方法，你可以动态地获取其他fixture的值。
        # from _pytest.fixtures import FixtureRequest
        #
        # # _fillfixtures是一个内部方法，在执行测试函数或fixture函数过程中，这个方法会查找与参数名称相匹配的fixture，
        # # 并将fixture的返回值注入到测试函数或其他fixture函数中。
        # # 其内部逻辑，如果fixture没有被调用，调用_get_active_fixturedef调用并缓存执行结果
        # # 但是在多线程运行case的场景下，会有线程同步问题，如果不做处理会导致fixture重入
        # # 所以要对_get_active_fixturedef限制并发调用
        # def sync_call(func):
        #     """
        #     装饰器，以相同的入参的调用函数时会被阻塞，使同时只能有一个并发
        #     """
        #     function_thread_lock_dict = defaultdict(threading.Lock)
        #
        #     @wraps(func)
        #     def run(obj, argname: str):
        #         with function_thread_lock_dict[argname]:
        #             return func(obj, argname)
        #
        #     return run
        #
        # FixtureRequest._get_active_fixturedef = sync_call(FixtureRequest._get_active_fixturedef)
        # FixtureRequest._fillfixtures = _fillfixtures

        def wraps(request, fixturedef: "FixtureDef", subrequest: "SubRequest"
                  ) -> None:
            return self._schedule_finalizers(request, fixturedef, subrequest)

        # 替换添加终结器的代码，用于记录fixturedef和对应的作用域
        FixtureRequest._schedule_finalizers = wraps

    def pytest_collection_modifyitems(self, session, config, items: list):
        # case分组的单元的mark标签字符

        for item in items:
            # 读取@pytest.mark.unit_group对case定义的分组单元
            units = self.get_marker_or_default(config, item, CASE_GROUP_UNIT_TAG)

            for u in units:
                # 标记item到应该归属的分组，
                groups = self._gener_item_group_key(item, u)

                for g in groups:
                    self.item_dict.setdefault(g, []).append(item)

        pass

        for item in items:
            lc = item.listchain()
            for c in lc:
                s = self.stack_map_case.setdefault(c, set())
                s.add(item)
        pass

        # from pytest import fail
        # def _compute_fixture_value(self, fixturedef: "FixtureDef") -> None:
        #     """
        #     Creates a SubRequest based on "self" and calls the execute method of the given fixturedef object. This will
        #     force the FixtureDef object to throw away any previous results and compute a new fixture value, which
        #     will be stored into the FixtureDef object itself.
        #     """
        #     # prepare a subrequest object before calling fixture function
        #     # (latter managed by fixturedef)
        #     argname = fixturedef.argname
        #     funcitem = self._pyfuncitem
        #     scope = fixturedef.scope
        #     try:
        #         param = funcitem.callspec.getparam(argname)
        #     except (AttributeError, ValueError):
        #         from _pytest.compat import NOTSET
        #         param = NOTSET
        #         param_index = 0
        #         has_params = fixturedef.params is not None
        #         fixtures_not_supported = getattr(funcitem, "nofuncargs", False)
        #         if has_params and fixtures_not_supported:
        #             msg = (
        #                 "{name} does not support fixtures, maybe unittest.TestCase subclass?\n"
        #                 "Node id: {nodeid}\n"
        #                 "Function type: {typename}"
        #             ).format(
        #                 name=funcitem.name,
        #                 nodeid=funcitem.nodeid,
        #                 typename=type(funcitem).__name__,
        #             )
        #             fail(msg, pytrace=False)
        #         if has_params:
        #             import inspect
        #             import py
        #             frame = inspect.stack()[3]
        #             frameinfo = inspect.getframeinfo(frame[0])
        #             source_path = py.path.local(frameinfo.filename)
        #             source_lineno = frameinfo.lineno
        #             rel_source_path = source_path.relto(funcitem.config.rootdir)
        #             if rel_source_path:
        #                 source_path_str = rel_source_path
        #             else:
        #                 source_path_str = str(source_path)
        #             from _pytest.compat import getlocation
        #             msg = (
        #                 "The requested fixture has no parameter defined for test:\n"
        #                 "    {}\n\n"
        #                 "Requested fixture '{}' defined in:\n{}"
        #                 "\n\nRequested here:\n{}:{}".format(
        #                     funcitem.nodeid,
        #                     fixturedef.argname,
        #                     getlocation(fixturedef.func, funcitem.config.rootdir),
        #                     source_path_str,
        #                     source_lineno,
        #                 )
        #             )
        #             fail(msg, pytrace=False)
        #     else:
        #         param_index = funcitem.callspec.indices[argname]
        #         # if a parametrize invocation set a scope it will override
        #         # the static scope defined with the fixture function
        #         paramscopenum = funcitem.callspec._arg2scopenum.get(argname)
        #         if paramscopenum is not None:
        #             scope = scopes[paramscopenum]
        #
        #     subrequest = SubRequest(self, scope, param, param_index, fixturedef)
        #
        #     # check if a higher-level scoped fixture accesses a lower level one
        #     subrequest._check_scope(argname, self.scope, scope)
        #     # try:
        #     #     # call the fixture function
        #     #     # fixturedef.execute(request=subrequest)
        #     # finally:
        #     # self._schedule_finalizers(fixturedef, subrequest)
        #     # print(f"fixturedef：{fixturedef} 依赖：{subrequest.node}")
        #     stack_map_setup.setdefault(subrequest.node, []).append(fixturedef)
        #
        # def _getnextfixturedef(self, argname: str) -> "FixtureDef":
        #     fixturedefs = self._arg2fixturedefs.get(argname, None)
        #     if fixturedefs is None:
        #         # we arrive here because of a dynamic call to
        #         # getfixturevalue(argname) usage which was naturally
        #         # not known at parsing/collection time
        #         assert self._pyfuncitem.parent is not None
        #         parentid = self._pyfuncitem.parent.nodeid
        #         fixturedefs = self._fixturemanager.getfixturedefs(argname, parentid)
        #         # TODO: Fix this type ignore. Either add assert or adjust types.
        #         #       Can this be None here?
        #         # self._arg2fixturedefs[argname] = fixturedefs  # type: ignore[assignment]
        #     # fixturedefs list is immutable so we maintain a decreasing index
        #     index = self._arg2index.get(argname, 0) - 1
        #     if fixturedefs is None or (-index > len(fixturedefs)):
        #         raise FixtureLookupError(argname, self)
        #     # self._arg2index[argname] = index
        #     return fixturedefs[index]
        #
        # def _get_active_fixturedef(self, argname: str
        #                            ) -> Union["FixtureDef", PseudoFixtureDef]:
        #     try:
        #         return self._fixture_defs[argname]
        #     except KeyError:
        #         try:
        #             fixturedef = _getnextfixturedef(self, argname)
        #         except FixtureLookupError:
        #             if argname == "request":
        #                 cached_result = (self, [0], None)
        #                 scope = "function"  # type: _Scope
        #                 return PseudoFixtureDef(cached_result, scope)
        #             raise
        #     _compute_fixture_value(self, fixturedef)
        #     return fixturedef
        # for item in items:
        #     request = item._request
        #     fixturenames = getattr(item, "fixturenames", request.fixturenames)
        #     for argname in fixturenames:
        #         if argname not in item.funcargs:
        #             # item.funcargs[argname] = _get_active_fixturedef(request, argname)
        #             f = _get_active_fixturedef(request, argname)
        #             # print(f)
        # arr = []
        # items = [data for data in items]
        # # items.insert(0, None)
        # # items.append(None)
        # for i in range(len(items) - 1):
        #     item = list(has_stack_level_change(items[i], items[i + 1]))
        #     item.append(items[i])
        #     arr.append(tuple(item))

        # def _next_buttom_node(arr, index):
        #     """
        #     返回下一个底层节点的索引
        #     例如arr列表的第{index}条case是模块N的第一条case，返回值就是模块N内第一个类的第一条case
        #
        #     :param arr:  节点缩进关系的列表
        #     :param index:  起点位置（不包括）
        #     :return:  下一个底层节点的索引，找不到更底层的节点，则返回None
        #     """
        #
        #     bottom_layer = arr[index][0]
        #     for i in range(index + 1, len(arr)):
        #         if arr[i][0] > bottom_layer:
        #             return i
        #     return None
        #
        # def _next_top_node(arr, index, end):
        #     """
        #     返回下一个高层节点的索引
        #     例如arr列表的第{index}条case是模块N内第一个类的第一条case，返回值就是类的最后一条case
        #
        #     :param arr:  节点缩进关系的列表
        #     :param index:  起点位置
        #     :param end:  终点点位置（包括）
        #     :return:  下一个高层节点的索引，如果没有更高层的节点，即只有与当前节点平级的节点，返回{end}表示的记录
        #     """
        #     i = target = 0
        #     bottom_layer = arr[index][0]
        #     for i in range(index, end + 1):
        #         if arr[i][0] >= bottom_layer:
        #             target = i
        #         else:
        #             return target
        #     return end
        #
        # def gener_node_group(arr, start, end, setup: list, teardown: list):
        #
        #     start_stack = []
        #     end_stack = []
        #
        #     def set_stack(con_layer, only_layer, first, latest):
        #         """
        #
        #         :param con_layer:  共同的层级
        #         :param only_layer:  独有的层级
        #         :param first:  行首元素
        #         :param latest:  行尾元素
        #         :return:
        #         """
        #         stack_lenth = len(start_stack)
        #
        #         # 清除非共同前缀的部分
        #         if con_layer < stack_lenth:
        #             for i in range(con_layer, stack_lenth):
        #                 del start_stack[len(start_stack)-1]
        #         if con_layer < len(end_stack):
        #             for i in range(con_layer, stack_lenth):
        #                 del end_stack[len(end_stack)-1]
        #
        #         stack_lenth = len(start_stack)
        #         # 添加独有后缀部分
        #         for i in range(stack_lenth, only_layer - 1):
        #             start_stack.insert(i, first)
        #             end_stack.insert(i, latest)
        # # start_stack[arr[start][0]] = (start, arr[start])
        # # end_stack[arr[end][0]] = (end, arr[end])
        # layer = arr[0][0]
        # only_layer = arr[0][1]
        # set_stack(layer, only_layer, (0, arr[0]), (len(arr) - 1, arr[len(arr) - 1]))
        #
        # layer = 0
        # for i in range(start + 1, end):
        #     if arr[i][0] == start_stack[layer][1][0]:
        #         setup.append((start_stack[layer][0], i))
        #         teardown.append((i, end_stack[layer][0]))
        #     elif arr[i][0] > start_stack[layer][1][0]:
        #         layer = arr[i][0]
        #         only_layer = arr[i][2]
        #         set_stack(layer, only_layer, None, None)
        #
        #         setup.append((start_stack[layer][0], i))
        #         teardown.append((i, end_stack[layer][0]))
        #         # 对于某层只有2个元素，第2个元素会要求行尾在行尾前执行，改为行首在行尾前执行
        #         latest = teardown[len(teardown) - 1]
        #         if latest[0] == latest[1]:
        #             teardown.pop()
        #             teardown.append((start_stack[layer][0], end_stack[layer][0]))
        #
        #         # 更新双端
        #         layer = arr[i][0]
        #         only_layer = arr[i][2]
        #         top = _next_top_node(arr, i, end)
        #         set_stack(layer, only_layer, (i, arr[i]), (top, arr[top]))
        #
        #     elif arr[i][0] < start_stack[len(start_stack) - 1][1][0]:
        #         layer = arr[i][0]
        #         only_layer = arr[i][1]
        #         top = _next_top_node(arr, i, end)
        #         set_stack(layer, only_layer,  (i, arr[i]), (top, arr[top]))
        #
        #         setup.append((start_stack[layer][0], i))
        #         teardown.append((i, end_stack[layer][0]))
        # next_end = _next_top_node(arr, i, end)
        # if next_end == i:
        #     continue
        # elif next_end < end:
        #     gener_node_group(arr, i, next_end, setup, teardown)
        #     gener_node_group(arr, next_end + 1, end, setup, teardown)
        # elif next_end >= end:
        #     gener_node_group(arr, i, next_end, setup, teardown)
        # while True:
        #     sub_start = _next_buttom_node(arr, start)
        #     if sub_start is not None:
        #         for i in range(start + 1, sub_start + 1):
        #             setup.append((start, i))
        #         gener_node_group(arr, sub_start, end, setup, teardown)
        #     else:
        #         next_end = _next_top_node(arr, start, end)
        #         if next_end - start > 2:
        #             for i in range(start + 1, next_end - 1):
        #                 teardown.append((i, next_end))
        #             gener_node_group(arr, sub_start, end, setup, teardown)
        # next_end = start
        # while True:
        #     next_end = _next_top_node(arr, next_end, end)
        #     if next_end < end:
        #         groups.append((start, next_end))
        #         next_end += 1
        #     else:
        #         break
        #
        # while True:
        #     # 搜索子组的起点
        #     groups.append((start, next_end))
        #
        #     if sub_start is not None:
        #         groups.append((start, sub_start))
        # groups = []
        # setup = []
        # teardown = []
        # gener_node_group(arr, 0, len(arr) - 1, setup, teardown)
        # pass
        # def gener_node_group(arr, start, end, groups: list):
        #     # 记录当前组本身
        #     next_end = _next_top_node(arr, start, end)
        #
        #     # 记录可能存在的子组
        #     while True:
        #         # 搜索子组的起点
        #         sub_start = _next_buttom_node(arr, start)
        #         if sub_start is not None:
        #             # 搜索子组的终点
        #             tmp = _next_top_node(arr, sub_start, end)
        #             if next_end <= tmp:
        #                 # 子组的终点就是当前组的终点，说明当前组已经处理完毕，退出搜索
        #                 # 记录搜索到的子组
        #                 groups.append((start, sub_start, next_end))
        #                 # 子组内可以还有子组，把子组、子组的子组记录
        #                 gener_node_group(arr, sub_start, next_end, groups)
        #                 # return
        #             # else:
        #             # 子组之后还可以有其他子组，移动limit,使其搜索当前子组之后的子组
        #             start = tmp + 1
        #             gener_node_group(arr, sub_start, next_end, groups)
        #         else:
        #             # 没有子组
        #             next_start = start
        #
        #             # 处理前面是底层，后面都是高层
        #             up = [next_start, next_end]
        #             _ = next_end
        #             while True:
        #                 _ = _next_top_node(arr, _ + 1, end)
        #                 if _ <= end:
        #                     up.append(_)
        #
        #                 if _ >= end:
        #                     break
        #             groups.append(up)
        #
        #             if next_end < end:
        #                 gener_node_group(arr, next_end + 1, end, groups)
        #
        #             return
        #
        # groups = []
        # gener_node_group(arr, 0, len(arr) - 1, groups)
        # pass

    def _schedule_finalizers(self, request: FixtureRequest, fixturedef: "FixtureDef",
                             subrequest: "SubRequest") -> None:
        scope = subrequest.node
        # self.stack_map_fuxturedef.setdefault(scope, set()).add(fixturedef)
        # 记录作用域和对应的fixturedef的执行结果、终结器
        self.stack_map_fuxturedef.setdefault(scope, {})[fixturedef] = (fixturedef._finalizers, fixturedef.cached_result)

        request.session._setupstate.addfinalizer(
            functools.partial(fixturedef.finish, request=subrequest), scope
        )

    def pytest_runtest_teardown(self, item: Item, nextitem: Optional[Item]) -> None:
        # return True
        _update_current_test_var(item, "teardown")

        # 根据case的stack,更新作用域下未执行的case的记录，如果作用域下无待执行的case,说明作用域已完成，执行卸载操作
        lc = item.listchain()
        lc.reverse()
        for c in lc:
            s: set = self.stack_map_case.get(c)
            s.remove(item)
            logger.info(f"case: {item} 已完成，作用域： {c.nodeid} 下剩余任务数量为：{len(s)}")
            if len(s) == 0:
                logger.info(f"case: {item} 已完成，作用域： {c.nodeid} 作用域正在被卸载")
                item.session._setupstate._pop_and_teardown()
            else:
                item.session._setupstate.stack.pop()

        # item.session._setupstate.teardown_exact(item, nextitem)
        _update_current_test_var(item, None)

    def init_thread_env(self, item: Function):
        """
        初始化线程相关的case运行上下文，包括session._setupstate.stack、fixturedef
        :param item: 要运行的case
        :return:
        """
        # 获取倒序的stack
        lc = item.listchain()
        lc.reverse()

        # 存储作用域对应的stack
        self.stack_map_content = {}

        # 初始化session._setupstate.stack
        setupstate: SetupState = item.session._setupstate
        setupstate.stack = []
        setupstate._finalizers = {}
        for c in lc:
            stack_finalizers_tuple = self.stack_map_content.get(c)
            if stack_finalizers_tuple:
                setupstate.stack.append(c)
                finalizers = stack_finalizers_tuple[1]
                if finalizers.get(c):
                    setupstate.addfinalizer(finalizers.get(c), c)
            else:
                # 对应的stack没有缓存，说明对应stack还没有执行过，直接执行即可
                break

        # 初始化fixturedef
        for c in lc:
            map = self.stack_map_fuxturedef.get(c)
            if map:
                for fixturedef, t in map:
                    fixturedef._finalizers = t[0]
                    fixturedef.cached_result = t[1]
            else:
                break

    @staticmethod
    def run_one_test_item(self, session, item, nextitem=None):
        try:
            self.init_thread_env(item)
            # reports = item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=None)
            if session.shouldfail:
                raise session.Failed(session.shouldfail)
            if session.shouldstop:
                raise session.Interrupted(session.shouldstop)
        except Exception as e:
            raise e
        finally:
            with self.lock:
                self.tasks.remove(item)

    def check_all_in_group(self, performing, items):
        """
        检查剩余的待执行任务、执行中任务是否都在一个分组内

        :param performing: 执行中的任务
        :param items: 剩余的待执行任务
        :return: True|False，如果剩余任务都在一个分组内，返回为True
        """
        performing_in_group = self.item_map_exist.setdefault(performing, [])
        if len(performing_in_group) == 0:
            for group_task in self.group_tasks:
                try:
                    _ = group_task.index(performing)
                    performing_in_group.append(_)
                except ValueError as e:
                    performing_in_group.append(-1)

        for item in items:
            item_in_group = self.item_map_exist.setdefault(item, [])
            for i in range(len(performing_in_group)):
                if performing_in_group[i] == -1 or item_in_group[i] == -1:
                    performing_in_group[i] = -1

        return max(performing_in_group) >= 0

    @pytest.mark.tryfirst
    def pytest_runtestloop(self, session):
        print(f'pytest-group: 线程数({self.thread_count})')

        if session.testsfailed and not session.config.option.continue_on_collection_errors:
            raise session.Interrupted(
                "%d error%s during collection"
                % (session.testsfailed, "s" if session.testsfailed != 1 else "")
            )

        if session.config.option.collectonly:
            return True

        self.group_tasks = self.item_dict.values()
        with ThreadPoolExecutor(max_workers=self.thread_count) as executor:
            self.items = items = [i for i in session.items]
            next_task = None
            for i in range(len(items)):

                while True:
                    # 尝试遍历全部任务，找到一个可运行的任务
                    for item in items:
                        # 检查任务是否没有冲突，可运行
                        if self.check_task_permission(item):
                            next_task = item
                            break
                        else:
                            next_task = None

                    # 执行任务，或短暂等待后重新搜索不冲突的任务
                    if next_task:
                        # 要求有2个以上待执行的任务时，才能启动最前面的任务（启动任务的函数需要当前任务和下一个任务才能正确的运行fixture,否则会每个任务都运行一次fixture），
                        # 当只剩下最后一个任务时，需要将最后一个任务加入待执行的任务的队列
                        self.add_exec_tasks(executor, session, next_task, len(items) == 1)
                        break
                    else:
                        # 存在一种可能，若执行中任务列表中只剩一个任务，由于pytest机制的原因，需要2个任务才能执行，所以任务队列中剩余的最后一个任务不会被执行。
                        # 但若是剩余未放到执行中任务列表的任务全部属于一个分组，会因为分组互斥，不会被调度执行，导致任务总也不执行完
                        # 所以需要检查剩余任务是否都在一个分组中，如果是，就只需要把剩余任务的第一个加入到执行中任务队列即可
                        if len(self.tasks) == 1 and self.check_all_in_group(self.tasks[0], items):
                            self.add_exec_tasks(executor, session, items[0], len(items) == 1)
                            break
                        # 如果找不到可以运行的任务，就先稍等一下
                        time.sleep(0.1)
        return True

    def check_task_permission(self, next_task):
        """
        检查任务是否可执行，确保任务不会因为业务逻辑冲突与其他任务发生互斥

        :param next_task:  计划下一个要运行的任务
        :return:  True|False,无冲突时为True
        """
        return self._check_task_group_permission(next_task) and \
               self._check_task_resource_permission(next_task)

    def is_notconcurrent_task(self, task):
        """
        检查任务是否为不接受并发的notconcurrent任务
        :param task:
        :return: True|False,不接受并发的任务为True
        """
        notconcurrent = self.is_notconcurrent.get(task)
        if not notconcurrent:
            notconcurrent = task.get_closest_marker(NOTCONCURRENT)
            notconcurrent = notconcurrent is not None
        self.is_notconcurrent[task] = notconcurrent
        return notconcurrent

    def _check_task_resource_permission(self, next_task):
        """
        检查任务是否有会与正在执行的任务的任务组是否存在使用相同的但不可共享的资源

        :param next_task:  计划下一个要运行的任务
        :return:  True|False,无冲突时为True
        """
        return True
        # 暂未实现

    def _check_task_group_permission(self, next_task):
        """
        检查任务是否有会与正在执行的任务存在任务组互斥冲突。

        :param next_task:  计划下一个要运行的任务
        :return:  True|False,无冲突时为True
        """
        # 检查计划运行的任务item在各个任务组中的索引，如果索引为-1即不存在于任务组中
        plan_task_exists = self.item_map_exist.setdefault(next_task, [])
        if len(plan_task_exists) == 0:
            for group_task in self.group_tasks:
                try:
                    _ = group_task.index(next_task)
                    plan_task_exists.append(_)
                except ValueError as e:
                    plan_task_exists.append(-1)

        # 遍历当前正在运行的任务performing_task，检查与当前计划运行的任务item是否冲突
        # 如果有冲突，说明当前任务不能执行
        with self.lock:
            for performing_task in self.tasks:
                performing_task_exists = self.item_map_exist.setdefault(performing_task, [])
                for i in range(len(performing_task_exists)):
                    # 检查与当前计划运行的任务item是否冲突
                    plan_exists = plan_task_exists[i]
                    performing_exists = performing_task_exists[i]
                    if plan_exists == -1 or performing_exists == -1 or plan_exists < performing_exists:
                        continue
                    return False

            return True

    def add_exec_tasks(self, executor, session, next_task, last_task=False):
        """
        添加一个任务，并在合适的时候执行它（具体就是在有多个待执行的任务时才执行，或者在最后一个任务时批量把全部的都添加完）
        :param next_task:
        :return:
        """

        def run_generic_task():
            with self.lock:
                self.items.remove(next_task)
                self.tasks.append(next_task)
            executor.submit(self.run_one_test_item, self, session, self.task_order[self.task_index],
                            self.task_order[self.task_index + 1])
            self.task_index += 1

        def run_notconcurrent_task():
            # 等待到任务队列中的任务全部执行完毕，再将notconcurrent任务启动,并等待notconcurrent任务运行完成
            while 1 < len(self.tasks):
                time.sleep(0.1)

            run_generic_task()

            while 1 < len(self.tasks):
                time.sleep(0.1)

        def run_last_generic_task():
            with self.lock:
                self.tasks.append(next_task)
            executor.submit(self.run_one_test_item, self, session, self.task_order[self.task_index], None)
            self.task_index += 1

        def run_first_generic_task():
            with self.lock:
                self.items.remove(next_task)
                self.tasks.append(next_task)

        # print("--------------------------------------")
        # print(next_task)
        # print(self.tasks)
        # print("--------------------------------------")

        self.task_order.append(next_task)
        # 当前有2个以上的任务未被执行，就先启动倒数第2个任务，倒数第一个任务可能需要稍稍等一会才能启动
        if len(self.task_order) - self.task_index >= 2:
            # 如果下一个待执行的任务是notconcurrent非并发任务,就等待当前任务全部执行完毕，并阻塞的等待notconcurrent任务执行完成，不调度新任务
            if self.is_notconcurrent_task(self.task_order[self.task_index]):
                run_notconcurrent_task()
                return
            else:
                # 否则就是一个普通的任务，正常的执行就可以
                run_generic_task()

                if last_task:
                    # 是全局的最后一个任务,就把队列中全部的任务都启动
                    run_last_generic_task()
        else:
            # 执行任务记录列表数小于2，说明是全局的第一个任务
            # 是全局的第一个任务，由于不够2个任务，不足以满足启动pytest case需要的当前任务、下一个任务，就先啥都不做，等待凑齐再启动任务
            run_first_generic_task()
            return

    def _gener_item_group_key(self, item, group_unit, groups=None) -> list:
        """
        计算出item所属分组的key
        :param item:
        :param group_unit: 分组单元单位，可取值例如function/class/module等
        :param groups: 已经生效的分组，如果为None则忽略此入参
        :return: item所属的分组，list
        """
        group_marker = item.get_closest_marker(CASE_GROUP_TAG)
        if group_marker:
            return group_marker.args

        out = None
        if group_unit == "module":
            out = getattr(item, group_unit)
        elif group_unit == "class":
            out = getattr(item, "cls")
        elif group_unit == "function":
            out = getattr(item, "_pyfuncitem")

        if not out:
            return [item]
        else:
            return [out]

    # @staticmethod
    # def get_or_create_marker(item, tag, *args, **kwargs):
    #     """
    #     从item获取mark标签，如果item没有指定的mark标签，就先为item创建，然后再返回
    #
    #     :param item:
    #     :param tag: 要获取（创建）mark标签
    #     :return:
    #     """
    #     marker = item.get_closest_marker(tag)
    #     if not marker:
    #         marker = pytest.mark.__getattr__(tag)(*args, **kwargs)
    #         item.add_marker(marker)
    #     return item.get_closest_marker(tag)

    @staticmethod
    def get_marker_or_default(config, item, tag) -> Tuple[str]:
        """
        从item获取mark标签字符串元组，如果item没有指定的mark标签，获取默认标签

        :param config:
        :param item:
        :param tag: 要获取（创建）mark标签
        :return: 获取
        """
        marker = item.get_closest_marker(tag)
        if not marker:
            return (parse_config(config, tag),)
        return marker.args
