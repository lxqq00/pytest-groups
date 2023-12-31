import functools
import os
import threading
import time
from collections import defaultdict

from concurrent.futures.thread import ThreadPoolExecutor
from typing import Tuple, Optional

import pytest
from _pytest.fixtures import FixtureDef, SubRequest, FixtureRequest
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


# def has_stack_level_change(item, nextitem):
#     """
#     检查两个入参的堆栈层级是否有变化
#     例如：
#     has_stack_level_change("/a/b","/a/c") -> 1,2，0,"/a",False
#     has_stack_level_change("/a/b","/a/c/d") -> 1,2，1,"/a/c",True
#     has_stack_level_change("/a/b/c","/a/b/d") -> 2,3，0,"/a/b",Fasle
#     has_stack_level_change("/a/b/c","/a/d") -> 1,3，-1,"/a",True
#     has_stack_level_change("/a/b/c","/a/d/e") -> 1,3,0,"/a/d",True
#     :param item: 当前任务
#     :param nextitem: 下一个任务
#     :return: tuple(共同前缀层数，item任务层数，item层数-next任务层数，共同前缀，是否发生fixture作用域变化)
#     """
#     if item is None:
#         item_collectors = []
#     else:
#         item_collectors = item.listchain()
#     if nextitem is None:
#         needed_collectors = []
#     else:
#         needed_collectors = nextitem.listchain()
#
#     item_layer = len(item_collectors)
#     diff_layer = item_layer - len(needed_collectors)
#
#     prefix = 0
#     while item_collectors[:prefix] == needed_collectors[:prefix]:
#         prefix += 1
#     prefix += -1
#
#     if item_collectors == [] or needed_collectors == []:
#         prefix_str = ""
#     else:
#         iter = item_collectors[:prefix]
#         iter = map(lambda x: x.name, iter)
#         prefix_str = reduce(lambda o, n: o + f",{n}", iter)
#
#     return prefix, item_layer, diff_layer, prefix_str, (diff_layer != 0 or prefix + 1 != item_layer)


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

        def _schedule_finalizers(request: FixtureRequest, fixturedef: "FixtureDef",
                                 subrequest: "SubRequest") -> None:
            scope = subrequest.node

            with self.lock:
                # 记录作用域和对应的fixturedef的执行结果、终结器
                self.stack_map_fuxturedef.setdefault(scope, {})[fixturedef] = (
                    fixturedef._finalizers, fixturedef.cached_result)

            request.session._setupstate.addfinalizer(
                functools.partial(fixturedef.finish, request=subrequest), scope
            )

        # 替换添加终结器的代码，用于记录fixturedef和对应的作用域
        FixtureRequest._schedule_finalizers = _schedule_finalizers

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
        # # 但是在多线程运行case的场景下，会有线程同步问题(同一个fixture在不同线程中同时被执行，有不同的执行结果)
        # # 所以要对_get_active_fixturedef限制并发调用
        def sync_call(func):
            """
            装饰器，以相同的入参的调用函数时会被阻塞，使同时只能有一个并发
            """
            function_thread_lock_dict = defaultdict(threading.Lock)

            @functools.wraps(func)
            def run(obj, argname: str):
                with function_thread_lock_dict[argname]:
                    return func(obj, argname)

            return run

        FixtureRequest._get_active_fixturedef = sync_call(FixtureRequest._get_active_fixturedef)

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

        # 记录case和作用域的对应关系
        for item in items:
            lc = item.listchain()
            for c in lc:
                s = self.stack_map_case.setdefault(c, set())
                s.add(item)
        pass

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
                continue

        # 初始化fixturedef
        for c in lc:
            map = self.stack_map_fuxturedef.get(c)
            if map:
                for fixturedef, t in map.items():
                    fixturedef._finalizers = t[0]
                    fixturedef.cached_result = t[1]
            else:
                continue
        pass

    @staticmethod
    def run_one_test_item(self, session, item, nextitem=None):
        try:
            with self.lock:
                self.init_thread_env(item)

            item.config.hook.pytest_runtest_protocol(item=item, nextitem=None)
            if session.shouldfail:
                raise session.Failed(session.shouldfail)
            if session.shouldstop:
                raise session.Interrupted(session.shouldstop)
        except Exception as e:
            logger.exception(e)
            raise e
        finally:
            with self.lock:
                self.tasks.remove(item)

    # def check_all_in_group(self, performing, items):
    #     """
    #     检查剩余的待执行任务、执行中任务是否都在一个分组内
    #
    #     :param performing: 执行中的任务
    #     :param items: 剩余的待执行任务
    #     :return: True|False，如果剩余任务都在一个分组内，返回为True
    #     """
    #     performing_in_group = self.item_map_exist.setdefault(performing, [])
    #     if len(performing_in_group) == 0:
    #         for group_task in self.group_tasks:
    #             try:
    #                 _ = group_task.index(performing)
    #                 performing_in_group.append(_)
    #             except ValueError as e:
    #                 performing_in_group.append(-1)
    #
    #     for item in items:
    #         item_in_group = self.item_map_exist.setdefault(item, [])
    #         for i in range(len(performing_in_group)):
    #             if performing_in_group[i] == -1 or item_in_group[i] == -1:
    #                 performing_in_group[i] = -1
    #
    #     return max(performing_in_group) >= 0

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
                        self.add_exec_tasks(executor, session, next_task)
                        break
                    else:
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

    def add_exec_tasks(self, executor, session, next_task):
        """
        添加一个任务，并在合适的时候执行它
        :param next_task:
        :return:
        """

        def run_generic_task():
            with self.lock:
                self.items.remove(next_task)
                self.tasks.append(next_task)
            executor.submit(self.run_one_test_item, self, session, self.task_order[self.task_index],
                            None)
            self.task_index += 1

        def run_notconcurrent_task():
            # 等待到任务队列中的任务全部执行完毕，再将notconcurrent任务启动,并等待notconcurrent任务运行完成
            while 1 < len(self.tasks):
                time.sleep(0.1)

            run_generic_task()

            while 1 < len(self.tasks):
                time.sleep(0.1)

        self.task_order.append(next_task)

        # 如果下一个待执行的任务是notconcurrent非并发任务,就等待当前任务全部执行完毕，并阻塞的等待notconcurrent任务执行完成，不调度新任务
        if self.is_notconcurrent_task(self.task_order[self.task_index]):
            return run_notconcurrent_task()
        else:
            # 否则就是一个普通的任务，正常的执行就可以
            return run_generic_task()

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
