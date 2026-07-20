# yang-xml-gen

由 YANG 模型生成可下发到设备的 NETCONF XML 的工具。

当前已完成 **第 1 步**（模型校验）、**第 2 步**（YANG → NETCONF XML 生成器，
含 edit-config 内节点删除）、**第 3 步**（JSON 模板脚手架 + 宽容类型序列化）、
**第 4 步**（choice/case 扁平化、rpc 调用、augment 固化）、**第 5 步**
（get / get-config 读取路径 + filter 构造 + `<with-defaults>` 参数）、
**第 6 步**（`<rpc-reply>` 反向解析：把设备回包 XML 还原成 JSON spec-data）。
工作流：先用 `--template` 生成空白 JSON 模板，填好数据后转成可下发的 XML；
读取侧用 `--wrap get-config`/`get` 配合 filter（可选 `<with-defaults>`）；
删除节点用 `_operation: delete`（见第 2 步「删除节点」）；
回包解析用 `--from-xml`（可选 `--data-only`）把 `<rpc-reply>` 还原成 JSON。

> 新用户从 [快速上手](#快速上手) 开始，可走通从模板到回包解析的完整闭环；
> 各功能的完整语义、库 API 与边界见下方分步文档。

## 目录结构

```
yang-xml-gen/
├── models/                 # 123 个 .yang 模型（IETF / OpenConfig / sysrepo）
├── scripts/
│   └── compile_models.py   # 批量编译与一致性校验脚本（第 1 步成果）
├── src/yang_xml_gen/       # XML 生成器代码（loader/schema/scaffold/builder/wrappers/xml_parser/cli）
├── examples/               # YAML/JSON 输入样例 + 模板/填值/rpc/get/delete + 回包样例
└── tests/                  # test_generator / test_scaffold / test_choice_rpc / test_filter / test_delete / test_parse
```

## 环境准备

需要 Python 3.x 和 pyang：

```bash
python -m pip install pyang
```

## 快速上手

从零到一个完整闭环的最短路径：**生成模板 → 填数据 → 转成可下发的 XML →
解析设备回包**。下面所有命令都假设你在仓库根目录 `yang-xml-gen/` 下运行。

### 设置 Python 路径

工具源码在 `src/` 下，运行前需把 `src/` 加入 Python 搜索路径。

**Linux / macOS / Git Bash：**

```bash
export PYTHONPATH=src
python -m yang_xml_gen.cli --list-modules        # 验证：打印已加载的模块名
```

**Windows PowerShell：**

```powershell
$env:PYTHONPATH="src"
python -m yang_xml_gen.cli --list-modules
```

> 后续命令统一写成 `python -m yang_xml_gen.cli ...`，请先按上面设好
> `PYTHONPATH`（当前 shell 有效）。也可写成单行，例如
> `PYTHONPATH=src python -m yang_xml_gen.cli ...`（bash）或
> `$env:PYTHONPATH="src"; python -m yang_xml_gen.cli ...`（PowerShell）。

### 1. 看看有哪些模块和顶层节点

```bash
python -m yang_xml_gen.cli --list-modules                 # 全部已加载模块
python -m yang_xml_gen.cli --roots ietf-interfaces        # 某模块的顶层 data 节点
```

`--roots` 返回空？说明该模块只定义 groupings、没有自己的 data tree，根节点
挂在别的模块下（例如 `openconfig-aaa` 的 `container aaa` 实际挂在
`openconfig-system` 的 `system` 根下——见下方「常见坑」）。

### 2. 生成空白 JSON 模板

不用手写 spec，让工具按 YANG schema 生成只含 config 节点的骨架：

```bash
python -m yang_xml_gen.cli --template ietf-interfaces.interfaces > ifcfg.json
python -m yang_xml_gen.cli --template ietf-interfaces.interfaces --include-state > ifread.json
```

`--include-state` 额外保留 `config false` 的 state 节点（读取场景常用）。

### 3. 填好数据，转成可下发的 XML

编辑 `ifcfg.json`，把占位值换成真实数据，然后：

```bash
# 默认：完整 <edit-config> 报文（可直接下发到设备）
python -m yang_xml_gen.cli ifcfg.json -o edit.xml

# 或只要裸 <config> 片段（嵌入到别的报文里）
python -m yang_xml_gen.cli ifcfg.json --wrap bare -o config.xml

# 写到文件（-o）或打到 stdout（省略 -o）
```

`wrap` 可选 `bare` / `edit-config` / `rpc` / `get-config` / `get`，也可写进
spec 的 `wrap` 字段。

### 4. 读取设备数据（get / get-config）

读取用 `wrap: get-config`（只读 running 配置）或 `wrap: get`（含运行态
state），配合 `filter` 收窄回包：

```bash
# subtree filter：只取 eth0 的配置
python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-subtree.json

# subtree filter + with-defaults：让设备把默认值也回传
python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-with-defaults.json

# 全量取回（无 filter；<get> 读 running + state，无 <target>）
python -m yang_xml_gen.cli examples/ietf-netconf-get-full.json
```

`filter` 省略 = 全量取回。subtree filter 里 `{}` 空容器 = 选择整棵子树，
带 key 的 entry = 内容匹配节点（RFC 6241 §6.2）。

### 5. 解析设备回包（`<rpc-reply>` → JSON）

设备返回 `<rpc-reply>` 后，反向解析回 JSON spec-data，可再喂回 `build()`
重新生成 XML（round-trip）：

```bash
# 默认：输出 {module, root, data} envelope（module/root 从 xmlns 自动推断）
python -m yang_xml_gen.cli reply.xml --from-xml

# 只输出 data 对象（多根全量回包，或只关心数据本身时）
python -m yang_xml_gen.cli reply.xml --from-xml --data-only

# 写到文件
python -m yang_xml_gen.cli reply.xml --from-xml -o reply.json
```

`<ok/>` 回包 → `{"ok": true}`；`<rpc-error>` → `{"rpc-error": [...]}`。

### 完整闭环示例

```bash
# 写：填好的 spec -> edit-config XML -> 下发设备
python -m yang_xml_gen.cli ifcfg.json --wrap edit-config -o edit.xml

# 读：构造 get-config 请求 -> 下发设备 -> 拿到 reply.xml
python -m yang_xml_gen.cli get.json --wrap get-config -o get.xml

# 解析：reply.xml -> JSON（可 diff、可二次编辑、可再 build 成 XML）
python -m yang_xml_gen.cli reply.xml --from-xml --data-only -o data.json
```

### 常见坑

- **`--roots` 返回空**：模块只有 groupings，根节点在别的模块。例如
  `openconfig-aaa` 的 `container aaa` 实际是 `openconfig-system.system` 的
  子节点——要取「aaa 全部信息」应针对 `openconfig-system` 的 `system` 根
  用 subtree filter `{"aaa": {}}`。
- **找不到模块**：`ModuleNotFoundError: No module named 'yang_xml_gen'` =
  没设 `PYTHONPATH=src`，或当前目录不是仓库根。
- **Windows 下输出路径**：别用 `/tmp/`，用相对路径（`-o out.xml`）或
  绝对路径（`-o E:\tmp\out.xml`）。
- **`get-config` vs `get`**：`get-config` 只读 running（配置数据）；`get`
  额外覆盖 `config false` 的运行态 state。「全部信息」通常要用 `get`。
- **identityref 前缀**：正向 builder 一律输出带前缀形态
  （`ianaift:ethernetCsmacd`），反向原样保留；round-trip 输入需用带前缀
  形态才能精确相等，裸 ident 反向后会带上前缀。

### 下一步

各功能的完整语义、库 API、边界与设计说明见下方分步文档（第 1 步到第 6 步）：
模型校验 → XML 生成器 → 模板脚手架 → choice/rpc/augment → get/get-config
读取路径 → `<rpc-reply>` 反向解析。

## 第 1 步：模型校验

### 运行

```bash
# 默认：每个模块只取最新 revision，一起编译
python scripts/compile_models.py

# 编译目录下所有 revision（含同一模块的多个版本）
python scripts/compile_models.py --all

# 只编译指定模块（取其最新 revision，可重复 --module）
python scripts/compile_models.py --module openconfig-interfaces --module ietf-interfaces

# 输出机器可读的 JSON 报告
python scripts/compile_models.py --json report.json
```

退出码非 0 表示存在 error，可接入 CI 作为模型集合的回归检查。

### 为什么默认只编译最新 revision

`models/` 里 15 个 OpenConfig 模块同时保留了多个 revision（例如
`openconfig-rpc` 有 5 个版本）。把同一模块的多个版本一起喂给 pyang 会触发
`DUPLICATE_CHILD_NAME`——两个 revision 对同一节点重复 augment，pyang 拒绝
接受。这是"同目录放多版本"的副作用，**不是真正的依赖缺失**。

默认模式按模块名去重、保留最新 revision，共 102 个模块一起编译；`--all`
模式编译全部 124 个文件，用于复现/排查版本冲突。

### 当前结果

- **默认模式（101 模块）**：0 error，0 warning，全部干净编译，退出码 0。
- **`--all` 模式（123 文件）**：剩余的 error/warning 全部为多版本
  augment 冲突，属于预期行为。

依赖链层面（import 是否齐全、能否解析）**完全干净**，没有缺模块、缺
typedef、缺 grouping 的问题。后续 XML 生成器可以放心基于这套模型构建
schema 树。

> 备注：原先 `openconfig-itla@2025-07-01.yang` 存在一个真 error
> （`itla-group-config` grouping 为空，导致 key 的 leafref
> `../config/index` 找不到目标）。该模块没有任何其他模块 import 或引用，
> 属孤立且不完整，已删除。

## 后续步骤

第 2 步起将实现 YANG → XML 生成器，规划见项目根的讨论。第 1 步已确保：
模型可被 pyang 正常加载与校验，import 链完整，为生成器提供了可信输入。

## 第 2 步：YANG → NETCONF XML 生成器

基于第 1 步加载好的模型，实现由结构化数据（YAML）生成可直接下发到设备的
NETCONF XML。当前已覆盖 `container` / `list` / `leaf` / `leaf-list`，
以及 identityref、enumeration、boolean 等常用类型；`choice`/`case` 扁平化
与 `rpc` 调用见第 4 步，`action`、`notification` 暂未支持。

### 安装依赖

```bash
python -m pip install pyang pyyaml
```

### 命令行用法

输入是一个 YAML 文件，描述要生成的根节点和数据：

```yaml
# examples/ietf-interfaces-eth0.yaml
module: ietf-interfaces      # 根节点所在的 YANG 模块
root: interfaces             # 要生成的顶层数据节点
operation: merge             # 根元素的默认 operation（可选）
wrap: edit-config            # 输出形态：bare | edit-config
data:                        # 根节点的内容
  interface:
    - name: eth0
      type: ethernetCsmacd   # identityref，自动解析并加前缀
      description: uplink to core
      enabled: true
    - name: eth1
      type: ethernetCsmacd
      enabled: true
```

```bash
# 生成完整 edit-config 报文（默认）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-eth0.yaml

# 只生成裸 <config> 片段
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-eth0.yaml --wrap bare

# 写到文件
PYTHONPATH=src python -m yang_xml_gen.cli spec.yaml -o out.xml

# 查看已加载的模块 / 某模块的顶层节点（辅助写 spec）
PYTHONPATH=src python -m yang_xml_gen.cli --list-modules
PYTHONPATH=src python -m yang_xml_gen.cli --roots ietf-interfaces
```

输出示例（`--wrap edit-config`）：

```xml
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="101">
  <nc:edit-config nc:operation="merge">
    <nc:target><nc:running /></nc:target>
    <nc:config>
      <interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces" nc:operation="merge">
        <interface>
          <name>eth0</name>
          <type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">ianaift:ethernetCsmacd</type>
          <description>uplink to core</description>
          <enabled>true</enabled>
        </interface>
      </interfaces>
    </nc:config>
  </nc:edit-config>
</nc:rpc>
```

### 处理的 NETCONF XML 关键点

- **命名空间**：每个元素带其定义模块的 `xmlns`；子元素若与父同模块则继承，
  避免冗余声明。
- **identityref 前缀**：值的前缀指向定义该 identity **值**的模块（如
  `ianaift:ethernetCsmacd`，`ianaift` 属 `iana-if-type`），并在该 leaf 上
  声明对应 `xmlns`。前缀由生成器根据全局 identity 索引自动解析，无需手填。
- **key 顺序**：`list` 的 key leaf 总是按声明顺序排在最前。
- **operation 注入**：数据中用 `_operation: merge|replace|create|delete|remove`
  给单个元素加 `nc:operation`；根元素可用 spec 顶层 `operation` 统一设置。
  `delete`/`remove` 可作用在 list entry / container / leaf 三种节点上，
  详见下方「删除节点」小节。
- **leaf-list**：映射为重复的同名兄弟元素。
- **boolean**：序列化为 YANG 的 `true`/`false`，而非 Python 的 `True`/`False`。

### 作为库使用

```python
from yang_xml_gen.loader import Loader
from yang_xml_gen.wrappers import bare_config, edit_config

ld = Loader()
data = {"interface": [{"name": "eth0", "type": "ethernetCsmacd", "enabled": True}]}
print(bare_config(ld, "ietf-interfaces", "interfaces", data))
print(edit_config(ld, "ietf-interfaces", "interfaces", data, operation="merge"))
```

### 删除节点（edit-config 内）

NETCONF 通过 `nc:operation="delete"`（RFC 6241 §7.2）在 `<edit-config>`
里删除节点。本工具用 `_operation` sentinel 表达，可作用在三种节点上：

| 删除对象 | 数据形态 | 生成的 XML | 语义 |
|---|---|---|---|
| **list entry** | `{interface: [{name: eth0, _operation: delete}]}` | `<interface nc:operation="delete"><name>eth0</name></interface>` | 删除 key 匹配的整条 entry；只需 key leaf |
| **container / 子树** | `{interface: [{name: eth0, type: ..., ipv4: {_operation: delete}}]}` | `<ipv4 nc:operation="delete"/>` | 删除整个子树，无子节点 |
| **leaf** | `{interface: [{name: eth0, type: ..., description: {_operation: delete}}]}` | `<description nc:operation="delete"/>` | 删除该 leaf，无 text |

`delete` 与 `remove` 行为一致，区别在服务端：`delete` 删不存在的节点会
`<error>`，`remove` 删不存在则静默成功（幂等）。两者在工具里序列化相同。

> **注意**：`delete`/`remove` 作用在 leaf 上时，sentinel 字典里**不能带值**
> ——`<description nc:operation="delete"/>` 本身就是完整指令。若对 leaf 用
> `merge`/`replace`/`create` 却只给 sentinel 不给值，工具会报错（这些
> operation 必须有具体值才有意义）。

工作流（四个 examples 对应四种删除场景）：

```bash
# A. 删除整条 interface entry（只填 key）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-delete-entry.json

# B. 删除单个 leaf（eth0 的 description）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-delete-leaf.json

# C. 删除整个子树（eth0 的 ipv4）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-delete-ipv4.json

# D. 一次删除多条 interface entry
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-delete-multiple-entries.json
```

#### 多 entry 删除

list 本就是数组，删除多条 entry 就是给数组里**每个要删的 entry 各自带
`_operation: delete`**——每条变成一个独立的 `<interface nc:operation="delete">`
兄弟元素，设备按文档顺序逐条处理。这是 NETCONF 的标准写法（RFC 6241 §7.2：
每个 list entry 是独立的 operation 目标），无需特殊语法。

```json
{
  "interface": [
    { "name": "eth0", "_operation": "delete" },
    { "name": "eth1", "_operation": "delete" },
    { "name": "eth2", "_operation": "delete" }
  ]
}
```

产出三个并列的删除指令（样例 D）：

```xml
<interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
  <interface nc:operation="delete"><name>eth0</name></interface>
  <interface nc:operation="delete"><name>eth1</name></interface>
  <interface nc:operation="delete"><name>eth2</name></interface>
</interfaces>
```

删除 entry 与普通 merge entry 可在同一个 edit-config 里混用：带
`_operation: delete` 的删、不带的按默认 operation（merge）处理。兄弟顺序
遵循数组顺序。

样例 B 产出：

```xml
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="302">
  <nc:edit-config>
    <nc:target><nc:running /></nc:target>
    <nc:config>
      <interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
        <interface>
          <name>eth0</name>
          <type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">ianaift:ethernetCsmacd</type>
          <description nc:operation="delete" />
        </interface>
      </interfaces>
    </nc:config>
  </nc:edit-config>
</nc:rpc>
```

`tests/test_delete.py`（16 项）覆盖：list entry 删除（含 key-only 与带非
key leaf 两种）、**多 entry 删除**（三条全删、顺序保留、删除与 merge 混用）、
container 删除（无子节点、namespace 为定义模块）、leaf 删除（无 text）、
`delete` vs `remove`、leaf 上 `merge`/`replace`/`create` sentinel 不带值时
报错、bare `build()` 与 CLI `--wrap edit-config` 两条路径。

### 测试

```bash
PYTHONPATH=src python -m unittest tests.test_generator -v
```

覆盖：命名空间与 key 顺序、identityref 前缀解析、boolean 序列化、operation
注入与非法值拒绝、缺 key / 未知子节点 / 未知根的错误处理、edit-config 报文
结构、裸片段无 rpc 包裹、片段可回解析。删除节点（list entry / container /
leaf，含 `delete`/`remove` 与负向）见 `tests/test_delete.py`。

### 模块构成

```
src/yang_xml_gen/
├── loader.py        # 加载全部模型，建 identity→模块 索引
├── schema.py        # 把 pyang 语句封装为 SchemaNode 树
├── xml_builder.py   # 数据+schema→XML（namespace/key/identityref/operation）
├── wrappers.py      # 裸片段 / edit-config / get-config 报文封装
├── cli.py           # YAML→XML 命令行入口
└── __main__.py      # 支持 python -m yang_xml_gen
```

### 已知限制（后续步骤）

- `when` 条件、`must` 约束、`mandatory`/`min-elements` 校验未实现。
- typedef 链只取最外层类型名，未递归到根类型。
- `action`、`notification` 未支持（见第 4 步边界）。
- filter 内容的语义校验交给设备（见第 5 步边界）。
- leaf-list 按值删除：`delete`/`remove` 作用在 leaf-list 单个值上需
  `<leaf nc:operation="delete">value</leaf>`（带值），与 leaf 的无值删除
  形态冲突，暂不支持；leaf-list 整体删除可用父容器的 `_operation: delete`。

## 第 3 步：JSON 模板脚手架 + 宽容类型序列化

第 2 步要求用户手写整个 spec，对复杂模型负担很重。第 3 步新增**空白 JSON
模板生成**：指定 module 和根节点，自动产出结构骨架，用户只负责往里填值。
**不做任何值校验**（range/length/pattern/enum 全部交给设备），输入一律当
string 容错处理。

### 工作流（两步）

```bash
# 1. 生成只含 config 节点的空白模板，重定向到文件
PYTHONPATH=src python -m yang_xml_gen.cli \
    --template ietf-interfaces.interfaces > ifcfg.json

# 2. 编辑 ifcfg.json 填入数据，再转成可下发的 NETCONF XML
PYTHONPATH=src python -m yang_xml_gen.cli ifcfg.json --wrap edit-config -o ifcfg.xml
```

生成含 state 节点的模板（用于 get / get-config 的 filter 骨架，想看到完整
数据树时）：

```bash
PYTHONPATH=src python -m yang_xml_gen.cli \
    --template ietf-interfaces.interfaces --include-state > ifread.json
```

### 模板规则

- **顶层外壳**：`{"module": ..., "root": ..., "data": {...}}`，与第 2 步的
  spec 形状一致，可直接喂给 `cli.py` 转换。
- **container** → `{}`，按 YANG 声明顺序递归子节点。
- **list** → 单元素占位列表 `[ {<key 填占位值>, 其余可配 leaf 留空串>} ]`；
  key 用 `"<key名>"` 作占位值，便于一眼分辨必须填的字段。
- **leaf / leaf-list** → 空串 `""`（leaf-list 用 `[""]`）。
- **state 节点（config false）**：默认**省略**（写配置时不该出现不可写字段）；
  `--include-state` 时保留，值留空。
- **过滤依据**：每个 `SchemaNode` 带 `is_config` 字段，取自 pyang 的
  `i_config`（grouping 展开后仍可靠）。

仓库内样例：
- `examples/ietf-interfaces-template.json` — 生成的空白 config 模板。
- `examples/ietf-interfaces-filled.json` — 填好数据后可直接转换的样例。

### 宽容类型序列化

第 3 步同时增强了 `xml_builder.py` 对特殊类型的处理（**仍不校验**，只保证
输出对设备合法）：

- **empty**：值忽略，序列化为 `<leaf/>`（无 text）。例如
  `ietf-ip` 的 `is-router` —— 它的存在即语义，值无意义。
- **decimal64**：直接 `str(value)`，不重格式化、不校验小数位。用户给
  `"12.50"` 就原样输出 `"12.50"`；给数字 `12.5` 就输出 `"12.5"`。
- **union**：不选支，当普通字符串输出，由用户负责填合法值。
- **int / float**：显式 `str()`，避免 Python 默认 repr 带来的歧义。

### 作为库使用

```python
from yang_xml_gen.loader import Loader
from yang_xml_gen.scaffold import generate_template, template_to_json

ld = Loader()
# 拿到 dict（可自行处理）
tpl = generate_template(ld, "ietf-interfaces", "interfaces")
# 拿到可直接写文件的 JSON 字符串
print(template_to_json(ld, "ietf-interfaces", "interfaces", include_state=True))
```

### 测试

```bash
# 全部测试（第 2 步 + 第 3 步）
PYTHONPATH=src python -m unittest discover -s tests -v
```

`tests/test_scaffold.py` 覆盖（13 项）：
- 模板外壳形状（module/root/data）与 JSON 可回解析。
- list 单元素占位、key 用 `<key名>` 占位值。
- 默认省略 state leaf（`oper-status`/`if-index`/`statistics` 等），
  `include_state=True` 时保留。
- leaf-list 占位为 `[""]`。
- 填好的模板可端到端转成带正确命名空间、key 在前、boolean 为 `true` 的 XML。
- empty leaf 序列化为无 text 元素（`ietf-ip` 的 `is-router`）。
- decimal64 保留用户字符串、也接受数字值。
- union leaf-list（`ietf-netconf-acm` 的 `group`）接受普通字符串。
- 未知 module / 未知根报错。

### 模块构成（更新）

```
src/yang_xml_gen/
├── loader.py        # 加载全部模型，建 identity→模块 索引
├── schema.py        # 把 pyang 语句封装为 SchemaNode 树（含 is_config）
├── scaffold.py      # 由 schema 生成空白 JSON 模板（config / 含 state 两种）
├── xml_builder.py   # 数据+schema→XML（namespace/key/identityref/operation/宽容类型）
├── wrappers.py      # 裸片段 / edit-config / get-config 报文封装
├── cli.py           # YAML/JSON→XML；--template 生成空白模板
└── __main__.py      # 支持 python -m yang_xml_gen
```

### 明确不做（边界）

- 不解析 typedef 链、不做 range/length/pattern/enum 校验——交给设备。
- 不自动推断裸 JSON 的 module/root——必须用 spec 外壳（即模板那层）。
- `get-config` 的 filter 仍由调用方构造（模板只是骨架辅助）。
- choice 互斥校验不做——扁平化后用户可填任意分支 leaf，是否互斥交给设备
  （见第 4 步）。

## 第 4 步：choice/case 扁平化、rpc 调用、augment 固化

第 4 步补齐之前未覆盖的 YANG 构造，使生成器能处理含 `choice`/`case` 的
模型，并支持发起 `rpc` 调用。`augment` 本就由 pyang 烘焙进 `i_children`，
无需额外处理——这里只补测试与文档将其固化。

### choice / case 扁平化

按 RFC 7951 §7.9，`choice`/`case` **不产生任何 XML 元素**：被选分支的 leaf
直接出现在父节点下。生成器据此把所有 case 的分支 leaf 平铺进父 `children`
（pyang 强制跨 case 同名 leaf 唯一，所以合并进一个 dict 不会撞名）。

例如 `ietf-netconf-acm` 的 `rule-type` choice 有三个 case（protocol-operation
/ notification / data-node），各含一个 leaf（`rpc-name` / `notification-name`
/ `path`）。扁平化后这三个 leaf 直接挂在 `rule` 下：

```yaml
module: ietf-netconf-acm
root: nacm
data:
  rule-list:
    - name: rl1
      rule:
        - name: r1
          module-name: "*"
          rpc-name: get           # 选填 protocol-operation 分支
          access-operations: exec
          action: permit
```

生成的 XML 里 `<rule>` 直接含 `<rpc-name>get</rpc-name>`，没有
`<rule-type>` 或 `<case>` 包裹。

模板里含 choice 的容器会把**所有分支 leaf 平铺列出**，用户选填其一即可。
**互斥校验交给设备**——与项目"遇错即停但不做语义校验"的原则一致。

### rpc 调用

rpc 按 RFC 6241 序列化为 `<rpc-name>input-children</rpc-name>`（**无**
`<input>` 包裹，input 参数直接坐在 rpc 元素下）。`--wrap rpc` 再外层包一个
`<rpc message-id="...">` 信封。

工作流（与 config 数据两步法对称）：

```bash
# 1. 生成 rpc input 的空白模板
PYTHONPATH=src python -m yang_xml_gen.cli \
    --template ietf-netconf.get-config > getcfg.json

# 2. 编辑 getcfg.json 填入 input 数据，再转成 rpc 报文
PYTHONPATH=src python -m yang_xml_gen.cli getcfg.json --wrap rpc
```

`getcfg.json` 形态（spec 外壳，`root` 为 rpc 名，`data` 为 input 参数）：

```json
{
  "module": "ietf-netconf",
  "root": "get-config",
  "wrap": "rpc",
  "message-id": 42,
  "data": {
    "source": { "running": true },
    "with-defaults": "report-all"
  }
}
```

生成的报文：

```xml
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="42">
  <get-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
    <source>
      <running />
    </source>
    <with-defaults xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-with-defaults">report-all</with-defaults>
  </get-config>
</nc:rpc>
```

要点：
- `source` 内部的 `config-source` choice 同样被扁平化——`candidate`/
  `running`/`startup` 三个 type-empty leaf 直接出现在 `<source>` 下，
  填 `true` 即表示选中（`<running/>` 无 text）。
- `with-defaults` 由 `ietf-netconf-with-defaults` augment 进来，namespace
  自动正确（见下）。
- pyang 对整个 rpc input 子树设 `i_config=None`（config 不适用于 rpc 参数），
  生成器强制将其视为 config，避免模板把 rpc 参数当 state 过滤掉。

`--roots ietf-netconf` 现在会列出全部 13 个 rpc，每个标 `rpc`：

```
rpc        get-config
rpc        edit-config
rpc        copy-config
...
```

### augment

pyang 在 `expand_2/3` 阶段已把 augment 目标的子节点直接塞进目标 `i_children`
（带 `i_augment` 标记，`i_module` 指向 augmenting 模块）。因此 augment 对
生成器**完全透明**——无需遍历 `augment` 语句，augmented 节点的 namespace
自动指向定义它的模块。

例如 `ietf-network-instance` 把 `bind-ni-name` augment 进 `ietf-interfaces`
的 `interface`，生成的 `<bind-ni-name>` 自动带
`xmlns="urn:ietf:params:xml:ns:yang:ietf-network-instance"`，而非
ietf-interfaces 的 namespace。少数 augment 目标位于 choice/case 内部节点，
扁平化后自然落在父 `children` 里，同样正确。

### anyxml

rpc input 中常见的 `anyxml`（如 `get-config` 的 `filter`）按 leaf 处理——
填字符串即作为文本内容输出，留空则省略。结构化 anyxml 子树仍是后续事项。

### 作为库使用

```python
from yang_xml_gen.loader import Loader
from yang_xml_gen.wrappers import rpc_call

ld = Loader()
data = {"source": {"running": True}, "with-defaults": "report-all"}
print(rpc_call(ld, "ietf-netconf", "get-config", data, message_id=42))
```

### 测试

```bash
# 全部测试（第 2/3/4 步）
PYTHONPATH=src python -m pytest -q
```

`tests/test_choice_rpc.py` 覆盖（10 项）：
- choice 扁平化：`rule-type` 三个分支 leaf 直接挂在 `rule` 下，无
  `rule-type`/`case`/`choice` 元素；模板里三个分支 leaf 同时列出。
- augment namespace：`bind-ni-name` 的 namespace 为
  `urn:ietf:params:xml:ns:yang:ietf-network-instance`；端到端 XML 中
  `<bind-ni-name>` 带该 namespace。
- rpc 调用：`get-config` 被建模为 `kind="rpc"`，input 子节点（`source`/
  `filter`/`with-defaults`）就位；`source` 内 `config-source` choice 亦被
  扁平化；序列化无 `<input>` 包裹，`running` 为 type-empty 无 text；
  `rpc_call` 外层包 `<rpc message-id=...>`；rpc input 在模板中不被当
  state 过滤；未知 rpc 名报 KeyError 并列出可用 rpc。

### 明确不做（第 4 步边界）

- **action**：嵌套在容器/list 内，调用需先写目标层级路径（含 list key），暂缓。
- **notification**：设备上报方向，工具不生成。
- **rpc output**：只生成 rpc 调用（input 方向），不生成响应。
- **choice 互斥校验**：扁平化后用户可填任意分支 leaf，是否互斥交给设备。
- **choice default case**：不做特殊处理（扁平化后 default 分支的 leaf 自然
  出现在模板里）。
- **when/must/mandatory/min-elements 校验**：仍不做。
- **typedef 链递归**：仍只取最外层类型名。
- **结构化 anyxml**：`anyxml` 按文本 leaf 处理，不解析 XML 子树。

## 第 5 步：get / get-config 读取路径 + filter 构造

第 2–4 步覆盖了写路径（edit-config）与 rpc 调用。第 5 步补齐**读路径**：
`<get-config>`（读某个 datastore 的配置）与 `<get>`（读 running 配置合并
运行态），并支持 NETCONF 的两种 filter（RFC 6241 §6）。

关键洞察：subtree filter 的"选择子树"内容与 `build()` 已能产出的数据片段
**完全同形**——只填 key 的 list entry 是"内容匹配节点"（选特定条目），空
容器是"选择节点"（选整棵子树）。因此 filter 内容直接复用 `build()`，无需
新写一套序列化逻辑。

### 工作流

```bash
# A. subtree filter：选 eth0 的全部配置（key-only entry = 内容匹配节点）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-subtree.json

# B. subtree filter：选 eth0 的 ipv4 子树（空容器 = 选择节点）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-ipv4-subtree.json

# C. xpath filter（无需 module/root，select 为 XPath 1.0 表达式）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-xpath.json

# D. 全量取（无 filter；<get> 读 running + 运行态，无 <target>）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-netconf-get-full.json

# E. subtree filter + with-defaults（让设备把默认值也一起回传）
PYTHONPATH=src python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-with-defaults.json
```

### spec 形态

读路径不使用 `data`，改用 `filter`（subtree）或 `filter-select`（xpath），
二者只能选一；都不给则全量取。`module`/`root` 仅 subtree filter 需要
（命名选择子树的根）；xpath filter 只要 `select` 字符串本身。

```json
{
  "module": "ietf-interfaces",
  "root": "interfaces",
  "wrap": "get-config",
  "target": "running",
  "message-id": 201,
  "filter": {
    "interface": [
      { "name": "eth0", "ipv4": {} }
    ]
  }
}
```

生成的报文（subtree，选 eth0 的 ipv4 子树）：

```xml
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="201">
  <nc:get-config>
    <nc:target>
      <nc:running />
    </nc:target>
    <nc:filter type="subtree">
      <interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
        <interface>
          <name>eth0</name>
          <ipv4 xmlns="urn:ietf:params:xml:ns:yang:ietf-ip" />
        </interface>
      </interfaces>
    </nc:filter>
  </nc:get-config>
</nc:rpc>
```

xpath 形态：

```xml
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="203">
  <nc:get-config>
    <nc:target><nc:running /></nc:target>
    <nc:filter type="xpath" select="/if:interfaces/if:interface[if:name=&quot;eth0&quot;]" />
  </nc:get-config>
</nc:rpc>
```

注意 xpath 的 `select` 属性值里若含 `"`，序列化时会被转成 `&quot;`（XML
属性用双引号包裹，内层双引号必须转义）——这是正确的 XML 行为，设备会还原
成原始表达式。

### 三种 filter 语义（RFC 6241 §6.2/§6.4）

| filter 形态 | spec 键 | 选择语义 |
|---|---|---|
| subtree，key-only entry | `filter: {interface: [{name: eth0}]}` | 内容匹配节点：选 `name=="eth0"` 的条目 |
| subtree，空容器 | `filter: {interface: [{name: eth0, ipv4: {}}]}` | 选择节点：选 eth0 条目下的整个 ipv4 子树 |
| subtree，多 entry | `filter: {interface: [{name: eth0}, {name: eth1}]}` | 多个内容匹配节点，各选一条 |
| xpath | `filter-select: "/if:interfaces/..."` | XPath 1.0 表达式，由设备求值 |
| 无 | （都不给） | 全量取 |

### `<get>` 与 `<get-config>` 的区别

- `<get-config>`（§7.5）：读指定 datastore（`<target>`），只返回配置数据。
  spec 用 `target` 键（默认 `running`）。
- `<get>`（§7.7）：无 `<target>`，返回 running 配置合并运行态（state 节点
  也返回）。用于读 operational state。

### `<with-defaults>` 参数（RFC 6243）

NETCONF 默认不回传节点的 schema 默认值（设备只发"被显式设置过"的节点）。
RFC 6243 的 `<with-defaults>` 参数让调用方控制这一行为，它由
`ietf-netconf-with-defaults` augment 进 `<get>`/`<get-config>` 的 input，
因此元素落在该模块自己的 namespace（不是 NETCONF base namespace）。

spec 用 `with-defaults` 键，取值为四种模式之一：

| 模式 | 语义（RFC 6243 §3） |
|---|---|
| `report-all` | 回传所有节点，包括未显式设置的默认值 |
| `report-all-tagged` | 同上，但默认值节点带标记（设备用 `default` 属性或专 namespace） |
| `trim` | 不回传值等于 schema 默认值的节点 |
| `explicit` | 只回传显式设置过的节点（NETCONF 默认行为，显式声明用） |

`<with-defaults>` 作为 `<get>`/`<get-config>` 的**最后一个**子元素，排在
`<target>` 和 `<filter>` 之后（与 augment 在模型里的位置一致）。它可与
任意 filter 形态组合，也可单独用于全量取。

```json
{
  "module": "ietf-interfaces",
  "root": "interfaces",
  "wrap": "get-config",
  "target": "running",
  "message-id": 205,
  "filter": { "interface": [{ "name": "eth0" }] },
  "with-defaults": "report-all"
}
```

生成的报文：

```xml
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="205">
  <nc:get-config>
    <nc:target>
      <nc:running />
    </nc:target>
    <nc:filter type="subtree">
      <interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
        <interface>
          <name>eth0</name>
        </interface>
      </interfaces>
    </nc:filter>
    <with-defaults xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-with-defaults">report-all</with-defaults>
  </nc:get-config>
</nc:rpc>
```

注意 `<with-defaults>` 用默认 namespace 声明（`xmlns=...`，无前缀），
而非 `nc:` 前缀——这正是 RFC 6243 §4.5.1 示例里的形态。非法模式（不在
上面四者之中）会在 wrapper 层抛 `ValueError`，不会产出报文。

### 作为库使用

```python
from yang_xml_gen.loader import Loader
from yang_xml_gen.wrappers import (
    get, get_config, subtree_filter, xpath_filter,
)

ld = Loader()

# subtree filter：选 eth0 的 ipv4 子树
f = subtree_filter(ld, "ietf-interfaces", "interfaces",
                   {"interface": [{"name": "eth0", "ipv4": {}}]})
print(get_config(target="running", filter_element=f, message_id=1))

# xpath filter
print(get_config(filter_element=xpath_filter("/if:interfaces"), message_id=2))

# 全量 <get>（running + state）
print(get(message_id=3))

# <with-defaults>：让设备回传默认值（RFC 6243）
print(get_config(filter_element=f, with_defaults="report-all", message_id=4))
print(get(with_defaults="trim", message_id=5))  # 也适用于 <get>
```

### 测试

```bash
# 全部测试（第 2/3/4/5 步）
PYTHONPATH=src python -m pytest -q
```

`tests/test_filter.py` 覆盖（28 项）：
- subtree filter：`type="subtree"` 属性；key-only entry 选特定条目且只含
  key leaf；空容器选整棵子树且无子节点；多 entry 各选一条。
- xpath filter：`type="xpath"` + `select` 属性；表达式原样透传不校验。
- get-config 报文：全量取无 `<filter>`；subtree/xpath filter 正确置于
  `<get-config>` 下；`target` 受 spec 控制。
- get 报文：无 `<target>`（§7.7）；subtree filter 嵌套子树正确；全量取
  无 `<filter>`。
- with-defaults（RFC 6243）：`<get-config>` 上置于 `<target>` 之后、
  `<get>` 上作为唯一子元素；带 filter 时置于 filter 之后；元素落在
  with-defaults namespace 而非 NETCONF base namespace；四种模式全部
  接受；省略时不产出该元素；非法模式抛 `ValueError`。
- CLI 端到端：get-config + subtree、get-config + xpath（无需 module/root）、
  get 全量、默认 target=running、同时给 `filter`+`filter-select` 报错、
  subtree filter 缺 module 报错、`with-defaults` 在 get-config/get 上
  透传且位置正确、非法 `with-defaults` 报错。

### 明确不做（第 5 步边界）

- **filter 内容的语义校验**：subtree filter 是否真的能选到东西、xpath 表达
  式是否合法，都由设备求值，工具不校验。
- **filter 的 `xmlns` 前缀绑定辅助**：xpath 表达式里用的命名空间前缀
  （如 `if:`）由用户负责与设备协商（通常通过 capability 交换约定），工具
  不自动注入前缀映射。
- **`report-all-tagged` 的标记格式**：`<with-defaults>` 只产出请求参数本身；
  该模式下响应里默认值节点如何被标记（`default` 属性 vs 专 namespace，
  RFC 6243 §3.2/§7.1）由设备实现决定，工具不涉及响应侧。
- **`<get>` 与 `<get-config>` 的响应解析**：只生成请求报文，不解析
  `<rpc-reply>`。

## 第 6 步：反向解析（`<rpc-reply>` → JSON）

第 1–5 步覆盖了写路径（edit-config / rpc）、读路径（get / get-config 请求报
文）。第 6 步补齐**响应侧**：把设备回的 `<rpc-reply>` XML 还原成正向工具消费
的 JSON spec-data 形态，使「请求 → 设备 → 回包」形成闭环——回包 JSON 可直接
再喂回 `build()` 重新生成 XML（round-trip）。

### 工作流

```bash
# 默认：输出 {module, root, data} envelope（module/root 从 payload 的 xmlns
# 自动推断，用户只给 XML 文件）
python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-reply.xml --from-xml

# 只输出 data 对象（适合多根的全量取回包，或只关心数据本身时）
python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-reply.xml --from-xml --data-only

# 写到文件
python -m yang_xml_gen.cli reply.xml --from-xml -o reply.json
```

### 三种 reply 形态的 JSON 输出

`parse_reply` 按 `<rpc-reply>` 的第一个子元素派发（RFC 6241）：

1. **`<data>`（数据回包，get / get-config 的典型响应）**
   - 单根 payload（subtree filter 回包，常见）：返回
     `{"module": ..., "root": ..., "data": {...}}`（`--data-only` 时只返回
     `data` 对象）。`module` / `root` 由 payload 根元素的 xmlns 经
     `Loader.module_by_namespace` 推断——遍历 `ctx.modules` 的 `namespace`
     语句建索引，namespace 跨模块唯一（冲突即第 1 步编译错误）。
   - 多根 payload（全量取回回包）：仅 `--data-only` 支持，返回
     `{root_name: data, ...}` 字典；envelope 形态是单根模型，多根时报
     `ParseError`（提示用 `--data-only` 或 subtree filter 收窄）。
   - 空 `<data/>`：`--data-only` 返回 `{}`；envelope 形态报 `ParseError`
     （无 module/root 可推断）。

2. **`<ok/>`（成功确认）**：返回 `{"ok": true}`，`--data-only` 不影响（无
   module/root 可附）。

3. **`<rpc-error>`（一个或多个错误）**：返回 `{"rpc-error": [...]}`，每个
   error 是 `<rpc-error>` 子元素（`error-type` / `error-tag` / `error-severity`
   / `error-message` / `error-path` / `error-info` 等）的通用结构化字典。这些
   子元素属 NETCONF base namespace、不被 YANG 建模，故走 schema-less 的
   pass-through 解析（有子元素→嵌套 mapping，重复同名→数组，无子元素→文本）。

未知 reply 形态（无 `<data>` / `<ok>` / `<rpc-error>`）→ `ParseError`。

### 类型还原规则

反向的类型还原与正向 `xml_builder._to_str` 对称：

| YANG leaf 类型 | XML 文本 | JSON 值 |
|---|---|---|
| `boolean` | `true` / `1` | `true` |
| `boolean` | `false` / `0` | `false` |
| `empty` | （无 text，存在即语义） | `true` |
| `identityref` | `prefix:ident` | 原样保留带前缀串 |
| 其余（string / enumeration / decimal64 / integer / …） | text | text 字符串 |

注意：正向 builder 对 identityref 一律输出带前缀形态（`ianaift:ethernetCsmacd`），
反向原样保留该串，所以 round-trip 输入需用带前缀形态才能精确相等；裸 ident
（`ethernetCsmacd`）正向也接受，但 round-trip 后会带上前缀。

### list / leaf-list → 数组（schema 驱动）

`<data>` 内的数据树按 schema 逐层 walk：

- **list** → 数组，每个 entry 是一个 mapping（按 schema 的 `is_list` 判定，
  与「同名兄弟出现几次」无关——即便只回 1 条 entry，也是单元素数组）。
- **leaf-list** → 数组，每个值是标量。
- **leaf / container** → 单值；同名兄弟出现 ≥2 次报 `ParseError`（不静默合并、
  不转数组），与正向 builder「每个 leaf/container 只产一次」对称。
- **未知 local name** → `ParseError`，列出该节点的合法子节点（与
  `SchemaNode.child` 的提示风格一致）。
- **augment 子节点**（如 `bind-ni-name`）已在 schema 里扁平化进父 `children`，
  按 local name 查表即得，namespace 差异不影响查找。

### `nc:operation` 往返

正向 builder 支持 `nc:operation`（删除节点用 `_operation: delete`）。反向对称
保留：

- container / list-entry 上的 `nc:operation` → entry mapping 里的 `"_operation"`
  键（值如 `"delete"` / `"merge"` / `"replace"`）。
- leaf 上的 `nc:operation="delete"`（或 `remove`）且无 text → 还原为哨兵
  `{"_operation": "delete"}`（正向 builder 消费此形态重新产出
  `<name nc:operation="delete"/>`）。
- leaf 上同时有值和 operation（正向工具本身产不出此形态）→ 只取值，丢弃
  operation。

注意：get / get-config 回包数据通常**不带** `nc:operation`，故主用例不受影响；
`nc:operation` 往返主要服务于「把 edit-config 风格的片段 XML 反向解析」这一
边缘场景。

### 库 API

```python
from yang_xml_gen.loader import Loader
from yang_xml_gen.xml_parser import parse_reply, ParseError

loader = Loader()
result = parse_reply(xml_string, loader, data_only=False)
# 单根 data 回包 -> {"module": ..., "root": ..., "data": {...}}
# 多根 data 回包 + data_only=True -> {root_name: data, ...}
# <ok/> -> {"ok": True}
# <rpc-error> -> {"rpc-error": [...]}
```

`ParseError(ValueError)` 与正向 `BuildError` 对称；payload 的 xmlns 匹配不到
已加载模块时，`Loader.module_by_namespace` 抛 `KeyError`（带 namespace 提示）。

### 端到端示例

用 `examples/ietf-interfaces-get-config-reply.xml`（一个真实的 `<get-config>`
回包，含 2 个 interface、identityref、boolean、empty leaf、嵌套 ipv4 子树）：

```bash
$ python -m yang_xml_gen.cli examples/ietf-interfaces-get-config-reply.xml --from-xml
{
  "module": "ietf-interfaces",
  "root": "interfaces",
  "data": {
    "interface": [
      {
        "name": "eth0",
        "description": "primary uplink",
        "type": "ianaift:ethernetCsmacd",
        "enabled": true,
        "link-up-down-trap-enable": "enabled",
        "ipv4": {
          "enabled": true,
          "forwarding": false,
          "mtu": "1500",
          "address": [
            {"ip": "10.0.0.1", "netmask": "255.255.255.0", "origin": "static"}
          ]
        }
      },
      {"name": "eth1", "type": "ianaift:ethernetCsmacd", "enabled": true}
    ]
  }
}
```

### 明确不做（第 6 步边界）

- **rpc-reply 元数据**（`message-id` 等）不保留：反向只关心 data/ok/error
  本身，protocol-level envelope 属性丢弃。
- **值合法性校验**：枚举值是否合法、identityref 是否存在、数值范围等交给设备，
  与正向「不做语义校验」一致。
- **`<rpc-error>` 的 `error-info` 深度建模**：通用 pass-through，不按 YANG
  解析（error-info 可能承载任意 YANG 片段，按需由调用方二次解析）。
- **leaf 带值时的 `nc:operation`**：正向工具产不出此形态，反向只取值、丢弃
  operation（见上）。
- **多根 data reply 的 envelope 形态**：envelope 是单根模型，多根仅支持
  `--data-only`（用 `--data-only` 或 subtree filter 收窄）。
- **裸片段反向**（无 `<rpc-reply>` 外层的裸数据树）：本轮不做，输入必须是
  `<rpc-reply>`。
