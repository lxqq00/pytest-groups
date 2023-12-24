
## 本插件的适用范围
本插件解决的问题：
1. 基于多线程的并发运行case
2. 基于@pytest.mark的用例分组，同一分组内的case保证运行的先后顺序
    ```python
    def test_01(login, xxx):
        print("执行test_01")
        time.sleep(3)


    def test_02(login, xxx):
        print("执行test_02")
        time.sleep(1)
    
    class TestDemoA():
    
        @pytest.mark.parametrize("n", range(10))
        def testa(self, n, login, xxx):
            print(f"开始执行A{n}")
            r = random.Random()
            sleep(r.randint(0, 3))
            print(f"A用例执行成功：{n}")
    ```
    
    以上case在多线程运行的场景下，test_01/test_02执行完成的先后顺序、数据驱动的testa的10条case执行的先后顺序是无法保证的。
    通过本插件可以保证先执行完test_01再执行test_02，testa的10条case按照参数n从0-9的顺序执行。
    
3. 处理了多线程场景fixture重复被调用的问题  
    目前市面上热度比较高的做并发执行case的插件有pytest-xdist/pytest-parallel，但是他们都有fixture重复调用的问题。  
    例如：
    ```python
    # conftest.py
    @pytest.fixture(scope='session')
    def login():
        print("fixture loggin start \n")
        return "fixture loggin"
    
    @pytest.fixture(scope='module')
    def xxx():
        print("fixture xxx start \n")
        return "fixture xxx"
        
    # test_demo2.py
    def test_01(login, xxx):
        print("执行test_01")
        time.sleep(3)
    
    def test_02(login, xxx):
        print("执行test_02")
        time.sleep(4)
     
    def test_03(login, xxx):
        print("执行test_03")
        time.sleep(5)
    
    def test_04(login, xxx):
        print("执行test_04")
        time.sleep(6)
    ```
    使用pytest-xdist将以上代码的case分配到2个进程中运行时，两个进程各自分别会运行一次名为login, xxx的fixture。      
    pytest-parallel运行时则是最差情况下每个case运行时都会把依赖的fixtrue都运行一遍，即login, xxx各运行4遍。      
    实际预期是login, xxx的fixture分别只执行一次，本插件实现了这种效果。        


本插件适用于需要并行运行pytest case的场景。case需要是IO密集型的任务，如果是CPU密集型（运行case时cpu使用率解决100%）的任务不适用于本插件


## 可用配置项
执行命令`pytest -h`可以查看pytest-groups插件的帮助信息    

有如下配置项：     
```bash
pytest-groups:
  --thread=THREAD       线程数量，每个线程都会用于启动一个分组，默认值：1
  --group-unit=GROUP_UNIT
                        自动分组单元单位，同一个单元单位内的case自动被规划到一个分组内，可选值：module(默认值)/class/function
```


## 配置分组线程数
通过--thread指定启动的线程数量，每个线程可以运行一个任务组，类似于以几个并发运行测试任务。   
需要注意同一个任务组的任务不会严格限制在同一个线程中执行，可能会插入其他任务组的任务的执行，只承诺同一个任务组内的任务会按照任务组内的顺序运行。

可以在配置文件pytest.ini声明线程数量，也可以在启动pytest时声明
```ini
[pytest]
addopts = -s --thread=4
```
```bash
pytest --thread=4
```

## 为case手动声明分组
标签@pytest.mark.group用于标注对象的分组，这个标签可以定义在module、class、function上，注意标签可以被继承，但是不会被覆盖。
例如：
```python
# xxx_test.py

#指定当前模块的case默认添加的标签
pytestmark = pytest.mark.group("分组5")

def test1():
    pass

@pytest.mark.group("分组1","分组4")
def test2():
    pass

@pytest.mark.group("分组1","分组2")
class Test_xxxx:
    def test3(self):
        pass
    @pytest.mark.group("分组3")
    def test4(self):
        pass
```
被`pytest.mark.group`标注分组后就不再受默认的上层的分组规则的影响，可以接受*args参数，入参可以是任意类型，入参的值相同的为一组。

以上的代码会有如下分组：
* 分组5
    - test1
* 分组4
    - test2
* 分组3
    - test4
* 分组2
    - test3
* 分组1
    - test2
    - test3

如果某case不需要分组,可以添加标签但不指定分组，这样就不会因为默认分组规则而自动为case添加分组，注意不同分组的任务（包括未分组的任务）运行的前后顺序无限制，会被随机调度，例如：
```python
@pytest.mark.group()
class Test_xxxx:
    pass
```

## 为case声明分组规则
实际测试工作的过程中，可能不需要精确的指定哪个case要归属到哪个分组，只需要同一个class或module内的用例被分为一个任务组，任务组内按照默认pytest的默认顺序执行即可。少数特殊的case再手动的声明所属分组。

这种情况可以以下4种方式之一指定分组规则,优先级为命令行参数 > pytest.ini中addopts的参数 > pytest.ini中option参数 > 什么都不配置的默认参数：
```ini
[pytest]
addopts = -s --group-unit=class
group-unit = class
```
```bash
pytest --group-unit=class
```
以上配置会自动将同一个class内的case自动分到一个分组内。

可选值包括module(默认值)/class/function，

取值为function时的效果等同于pytest case不做任何配置且启用并发执行case时的效果，不保证case执行完成的前后顺序。

取值为class时自动将同一个class内的case自动分到一个分组内，

同理取值为module时自动将同一个module内的case自动分到一个分组内，包括这个模块内的class中的case.


## 已知缺陷
使用本插件多线程执行case过程中，如果有n个分组，只会有n-1个线程运行。例如有a、b两个分组，则多线程运行时，只会有1个并发线程运行case。
其底层是因为pytest运行任务必须要知道当前想要运行的item和下一个要运行的item,case的teardown阶段会比较item、nextitem决定是否执行卸载操作。所以当有2个分组时，可以知道2个分组的第一个任务(a1、b1)都是需要执行的，所以会把2个任务加入到任务队列。
但只会执行第一个任务，需要再在任务队列中添加第3个任务才会运行第2个任务，item运行消耗的时间是不确定的，而此时第3个任务可能是任意分组的第2个任务，所以必须要把第一个任务执行完才能确定的确认第3个任务（分组a的后续任务）
所以最终只会有n-1个线程运行。
解决方法（待实现）：
根据任务组、item全局顺序，提前计算任务组内的下一个任务，例如a1/a2/a3,b1/b2/b3。

本插件不完全兼容pytest-ordering插件，pytest-ordering插件通过@pytest.mark.run(order=1)标示case的优先级，之后根据优先级对case的先后顺序进行排序，但是排序后的顺序还不是最后执行的顺序。
本插件就在最后执行case的步骤中工作，会根据本插件的顺序对具体的case再进行排序，所以case的最终执行顺序较大可能与pytest-ordering的顺序不一致。但是通过pytest-ordering排序靠前执行的，经过本插件再调度后仍然有比较可能靠前执行。

由于任务的调度顺序与默认顺序不一致，导致fixture的调用顺序也会与pytest默认的执行顺序不一致，pytest-ordering插件有同样的问题。

由于是并发执行case,会存在fixture或setup/treamdown方法被在多个case中同时被执行，因此相关方法需要是可重入、线程安全的，不要是基于某个全局变量才能装载卸载。


