# yang-xml-gen

**由 YANG 模型驱动的 NETCONF XML 生成器与反向解析器。**

[English](README.md) | [中文](README_zh.md)

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![pyang](https://img.shields.io/badge/depends%20on-pyang-orange.svg)

`yang-xml-gen` 把一个小的 YAML/JSON spec 转成可下发到设备的 NETCONF XML，并把
`<rpc-reply>` XML 反向解析回 JSON——全程基于你自己的 YANG 模型做 schema 驱动。你
只需描述「要什么数据」，工具负责解析命名空间、identityref 前缀、list key 顺序和
`nc:operation`。

**主要特性**

- **正向**：spec → `<edit-config>` / `<rpc>` / `<get-config>` / `<get>` XML，或裸
  `<config>` 片段。命名空间、identityref 前缀、list key 顺序均由 YANG 推导。
- **反向**：`<rpc-reply>` XML（或裸数据片段）→ JSON spec-data，可再喂回 `build()`
  重新生成 XML（round-trip）。
- **脚手架**：`--template` 按 schema 生成空白 JSON 骨架，无需手写 spec。
- **校验**：叶值按 YANG 类型约束（`range`/`length`/`pattern`/`enumeration`/
  `identityref`/`bits`/`union`/`decimal64`）检查，以**非阻断**的
  `YangValidationWarning` 抛出——设备仍是最终权威。
- **打包**：`pip install` 即得 `yang-xml-gen` 命令；附 `py.typed` 类型标记。

## 目录

- [安装](#安装)
- [快速上手](#快速上手)
- [命令行参考](#命令行参考)
- [Spec 文件格式](#spec-文件格式)
- [库 API](#库-api)
- [值校验](#值校验)
- [反向解析](#反向解析)
- [读取路径：get / get-config](#读取路径get--get-config)
- [打包发布](#打包发布)
- [测试](#测试)
- [已知限制](#已知限制)
- [项目结构](#项目结构)
- [许可证](#许可证)

## 安装

需要 **Python 3.10+** 和 **pyang**。两种安装方式：

### 方式 A：`pip install`（推荐）

```bash
# 在仓库根目录安装（editable 模式，改代码立即生效）
python -m pip install -e .

# 安装后得到 yang-xml-gen 命令
yang-xml-gen --list-modules --models-dir models
```

### 方式 B：`PYTHONPATH`（开发期，无需安装）

```bash
python -m pip install pyang pyyaml
export PYTHONPATH=src          # Linux / macOS / Git Bash
# Windows PowerShell: $env:PYTHONPATH="src"
```

### 关于 `models/` 目录

wheel **不打包** YANG 模型——它们是体积较大的上游产物。非 editable 的
`pip install` 后，`Loader()` 无法自动找到模型，会抛 `RuntimeError`。通过以下任一
方式显式指定 models 路径：

- CLI 参数：`--models-dir /path/to/models`
- 环境变量：`export YANG_XML_GEN_MODELS_DIR=/path/to/models`

**仓库内的 editable 安装是特例**：`__file__` 指向源码树，`Loader()` 仍能自动发现
仓库根的 `models/`。只有真正装进 `site-packages` 的非 editable 安装才触发「必须
显式指定」。标准 IETF / OpenConfig 模型的获取方式见
[`models/README.md`](models/README.md)。

仓库自带一个小的**示例模型** `models/example-toaster@2026-07-17.yang`，贯穿本
README 使用——它是提交进仓库的（不像其他上游模型那样被 `.gitignore` 忽略）。

## 快速上手

用自带的 `example-toaster` 模型走通完整闭环：
**探查 → 生成模板 → 填数据 → 转 XML → 解析回包**。所有命令在仓库根目录下运行。

> 用方式 A（`pip install`）的用户可把下面的 `python -m yang_xml_gen.cli` 换成
> `yang-xml-gen`。用方式 B 的请先设好 `PYTHONPATH=src`。

### 1. 探查模型

```bash
$ python -m yang_xml_gen.cli --list-modules | grep toaster
example-toaster

$ python -m yang_xml_gen.cli --roots example-toaster
container  toaster
rpc        make-toast
rpc        cancel-toast
```

### 2. 生成空白模板

```bash
python -m yang_xml_gen.cli --template example-toaster.toaster > examples/toaster-template.json
```

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "data": {
    "darkness": "",
    "toast-type": "",
    "mode": "",
    "label": ""
  }
}
```

### 3. 填好数据，转成 XML

把模板编辑成 [`examples/toaster-config.yaml`](examples/toaster-config.yaml)：

```yaml
module: example-toaster
root: toaster
operation: merge
wrap: edit-config
data:
  darkness: 7
  toast-type: wheat-bread
  mode: defrost
  label: Kitchen counter
```

生成 NETCONF XML：

```bash
$ python -m yang_xml_gen.cli examples/toaster-config.yaml
```

```xml
<?xml version='1.0' encoding='utf-8'?>
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="101">
  <nc:edit-config nc:operation="merge">
    <nc:target>
      <nc:running />
    </nc:target>
    <nc:config>
      <toaster xmlns="urn:example:toaster" nc:operation="merge">
        <darkness>7</darkness>
        <toast-type xmlns:toaster="urn:example:toaster">toaster:wheat-bread</toast-type>
        <mode>defrost</mode>
        <label>Kitchen counter</label>
      </toaster>
    </nc:config>
  </nc:edit-config>
</nc:rpc>
```

注意 identityref `toast-type` 自动带上 `toaster:` 前缀和对应的命名空间声明——你
只写裸 identity 名，前缀由全局 identity 索引解析。

### 4. 解析设备回包为 JSON

给定 [`examples/toaster-reply.xml`](examples/toaster-reply.xml)（toaster 的一个
`<get-config>` 回包）：

```bash
$ python -m yang_xml_gen.cli examples/toaster-reply.xml --from-xml
```

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "data": {
    "darkness": "7",
    "toast-type": "toaster:wheat-bread",
    "mode": "defrost",
    "label": "Kitchen counter"
  }
}
```

`module` 与 `root` 由 payload 的 `xmlns` 自动推断。结果可 round-trip——再喂回
`build()` 得到同样的 XML。

### 完整闭环

```bash
# 写：spec -> edit-config XML -> 下发设备
python -m yang_xml_gen.cli examples/toaster-config.yaml -o edit.xml

# 读：构造 get-config 请求，下发设备，拿到 reply.xml
# （请求构造见下方「读取路径」）

# 解析：reply.xml -> JSON（可 diff、可编辑、可再 build 成 XML）
python -m yang_xml_gen.cli examples/toaster-reply.xml --from-xml --data-only -o data.json
```

## 命令行参考

```bash
python -m yang_xml_gen.cli [spec] [options]
# pip 安装后：  yang-xml-gen [spec] [options]
# 或：         python -m yang_xml_gen [spec] [options]
```

| 参数 | 取值 | 说明 |
|---|---|---|
| `spec`（位置参数） | 路径 | YAML/JSON spec 文件（配合 `--from-xml`/`--from-fragment` 时为 XML 文件） |
| `-o`, `--output` | 路径 | 写入此文件（默认 stdout） |
| `--wrap` | `bare` \| `edit-config` \| `rpc` \| `get-config` \| `get` | 输出形态；覆盖 spec 的 `wrap` 键（默认 `bare`） |
| `--models-dir` | 路径 | 覆盖 models 目录 |
| `--list-modules` | — | 打印已加载模块名并退出 |
| `--roots` | `MODULE` | 打印某模块的顶层 data 节点并退出 |
| `--template` | `MODULE.ROOT` | 为 `MODULE.ROOT` 生成空白 JSON 模板并退出 |
| `--include-state` | — | 配合 `--template`：保留 `config false`（state）节点 |
| `--from-xml` | — | 把 `spec` 当作 `<rpc-reply>` XML 文件，反向解析成 JSON |
| `--from-fragment` | — | 把 `spec` 当作裸数据片段，反向解析成 JSON |
| `--data-only` | — | 配合 `--from-xml`/`--from-fragment`：只输出 `data`，不带 `{module, root, data}` 外壳 |

`--from-xml` 与 `--from-fragment` 互斥。

## Spec 文件格式

spec 是一个 YAML 或 JSON mapping，描述根节点及其内容。

```yaml
module: example-toaster      # 根节点所在的 YANG 模块
root: toaster                # 要生成的顶层节点
operation: merge             # 根元素的默认 nc:operation（可选）
wrap: edit-config            # 输出形态（可选；默认 bare，或见 --wrap）
message-id: 101              # rpc / get-config / get：message-id 属性
data:                        # 根节点的内容
  darkness: 7
  toast-type: wheat-bread
  mode: defrost
  label: Kitchen counter
```

| 键 | 是否必填 | 适用于 | 含义 |
|---|---|---|---|
| `module` | 正向必填 | 全部 | 根节点所在的 YANG 模块 |
| `root` | 正向必填 | 全部 | 顶层 data 节点（`wrap: rpc` 时为 rpc 名） |
| `data` | 正向必填 | edit-config / rpc / bare | 根节点的内容 |
| `wrap` | 可选 | 全部 | `bare` \| `edit-config` \| `rpc` \| `get-config` \| `get` |
| `operation` | 可选 | edit-config / bare | 根元素的默认 `nc:operation` |
| `message-id` | 可选 | rpc / get-config / get | `message-id` 属性 |
| `target` | 可选 | get-config | 要读的 datastore（默认 `running`） |
| `filter` | 可选 | get-config / get | subtree filter（spec-data 形态；需 `module`+`root`） |
| `filter-select` | 可选 | get-config / get | xpath filter（字符串；不需 `module`/`root`） |
| `with-defaults` | 可选 | get-config / get | RFC 6243 模式：`report-all` / `report-all-tagged` / `trim` / `explicit` |

`wrap: get-config` / `get` 时**不用** `data`——用 `filter`（subtree）或
`filter-select`（xpath）选择要取的内容；两者都省略则全量取。见
[读取路径](#读取路径get--get-config)。

### 删除节点（`_operation` sentinel）

用 `_operation` 键表达 `nc:operation="delete"`（RFC 6241 §7.2），可作用在三种节点上：

| 删除对象 | 数据形态 | 生成的 XML | 语义 |
|---|---|---|---|
| **list entry** | `{interface: [{name: eth0, _operation: delete}]}` | `<interface nc:operation="delete"><name>eth0</name></interface>` | 删除 key 匹配的 entry；只需 key leaf |
| **container / 子树** | `{... ipv4: {_operation: delete}}` | `<ipv4 nc:operation="delete"/>` | 删除整个子树，无子节点 |
| **leaf** | `{... description: {_operation: delete}}` | `<description nc:operation="delete"/>` | 删除该 leaf，无 text |

`delete` 与 `remove` 序列化相同；区别在服务端（`delete` 删不存在的节点会报错，
`remove` 幂等）。leaf 的删除 sentinel **不能带值**——`<leaf nc:operation="delete"/>`
本身就是完整指令。

## 库 API

所有函数在 `yang_xml_gen.*` 下。`Loader` 是入口——它加载 models 目录下的全部
`.yang`，并建 identity/namespace 索引。

```python
from yang_xml_gen.loader import Loader
loader = Loader()                       # 自动发现仓库 models/，或设 models_dir=...
loader = Loader(models_dir="/path/to/models")
```

### 构建器（`xml_builder.py`）

```python
from yang_xml_gen.xml_builder import build, build_fragment

build(loader, module_name, root, data, operation=None) -> ET.Element
build_fragment(loader, module_name, root, data, operation=None) -> str
```

`build` 返回 `ElementTree.Element`；`build_fragment` 是美化后的字符串便捷封装。

### 报文封装（`wrappers.py`）

```python
from yang_xml_gen.wrappers import (
    bare_config, edit_config, rpc_call, get, get_config,
    subtree_filter, xpath_filter,
)

bare_config(loader, module_name, root, data, *, operation=None) -> str
edit_config(loader, module_name, root, data, *,
            target="running", operation=None, message_id=101) -> str
rpc_call(loader, module_name, rpc_name, data, *, message_id=101) -> str

# 注意：get 和 get_config 是仅关键字参数（没有 loader/module/root 位置参数）。
get_config(*, target="running", filter_element=None,
           with_defaults=None, message_id=102) -> str
get(*, filter_element=None, with_defaults=None, message_id=103) -> str

subtree_filter(loader, module_name, root, data) -> ET.Element
xpath_filter(select: str) -> ET.Element
```

注意 `rpc_call` 的第三个位置参数是 `rpc_name`，不是 `root`。`with_defaults` 会校验
取值是否在 `("report-all", "report-all-tagged", "trim", "explicit")` 中，非法值抛
`ValueError`。

### 解析器（`xml_parser.py`）

```python
from yang_xml_gen.xml_parser import parse_reply, parse_fragment, ParseError

parse_reply(xml, loader, *, data_only=False) -> Any
parse_fragment(xml, loader, *, module=None, root=None, data_only=True) -> Any
```

> **易错点**：`parse_reply` 默认 `data_only=False`（返回 `{module, root, data}`
> 外壳），但 `parse_fragment` 默认 `data_only=True`（只返回 `data`）。两者故意相反。
> 若输入其实是 `<rpc-reply>`，`parse_fragment` 会抛 `ParseError`（请改用
> `parse_reply`）。

### 脚手架（`scaffold.py`）

```python
from yang_xml_gen.scaffold import generate_template, template_to_json

generate_template(loader, module, root, *, include_state=False) -> dict
template_to_json(loader, module, root, *, include_state=False, indent=2) -> str
```

list 的 key leaf 占位为 `"<key名>"`，其余 leaf 占位为 `""`，leaf-list 占位为
`[""]`。state（`config false`）节点默认省略，`include_state=True` 时保留。

### 校验器（`validator.py`）

```python
import warnings
from yang_xml_gen.validator import (
    YangValidationWarning, validate_value, emit_warnings,
)

validate_value(node, value, loader) -> list[str]   # [] = 合法；永不抛
emit_warnings(node, value, loader) -> None         # validate_value + warnings.warn

# 严格模式：把违例转成硬错误
warnings.filterwarnings("error", category=YangValidationWarning)
```

`YangValidationWarning` 是 `UserWarning` 子类——可按类别过滤以静默或升级。见
[值校验](#值校验)。

## 值校验

每个叶值都会按其 YANG 类型约束检查，违例经 `warnings.warn` 抛出
`YangValidationWarning`——**非阻断**。`build()` 仍返回 Element，`parse_*` 仍返回
dict。设备仍是最终权威；校验只是把明显的拼写错提前到本机告警。

**覆盖的约束**（每条违例一条警告，指明约束类型）：

| 约束 | 触发条件 | 示例（针对 `example-toaster`） |
|---|---|---|
| `range` | 数值 / decimal64 越界 | `darkness=99`（range `1..10`） |
| `length` | 字符串长度越界 | `label=""`（length `1..32`） |
| `pattern` | 字符串不匹配 XSD 正则 | IPv4 leaf 填 `"999.999.999.999"` |
| `enumeration` | 值不在枚举集 | `mode="nuclear"`（只有 `regular`/`defrost`/`reheat`） |
| `identityref` | identity 不存在，或存在但不派生自 leaf 的 base | `toast-type="toaster:cold-pizza"` 合法；不存在的 identity 会警告 |
| `decimal64` | 小数位超过 `fraction-digits` | `fraction-digits=2` 时填 `"12.345"` |
| `bits` | bit 名不在声明集 | `access-operations="create,bogus"` |
| `union` | 不匹配任何成员类型 | 日期 union leaf 填 `"not-a-date"` |

校验是**双向**的：`build()` 在 `_format_value` 处检查，
`parse_reply()`/`parse_fragment()` 在 `_coerce_value` 处检查——共用同一个
`emit_warnings`，两侧行为一致。

**示例**——把越界的 `darkness` 和非法 `mode` 写进 `bad.json`：

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "wrap": "bare",
  "data": { "darkness": 99, "mode": "nuclear" }
}
```

```bash
$ python -m yang_xml_gen.cli bad.json
```

```
.../xml_builder.py:307: YangValidationWarning: leaf 'darkness': value '99' violates a 'uint8' constraint (range/length/pattern/enumeration)
.../xml_builder.py:307: YangValidationWarning: leaf 'mode': value 'nuclear' violates a 'enumeration' constraint (range/length/pattern/enumeration)
<?xml version='1.0' encoding='utf-8'?>
<toaster xmlns="urn:example:toaster">
  <darkness>99</darkness>
  <mode>nuclear</mode>
</toaster>
```

两条警告，但 XML 照常产出。

**严格模式**——升级为硬错误（CLI：`-W
error::yang_xml_gen.validator.YangValidationWarning`）：

```python
import warnings
from yang_xml_gen.validator import YangValidationWarning
warnings.filterwarnings("error", category=YangValidationWarning)
# 此后非法叶值会抛 YangValidationWarning 而非警告。
```

**跳过项**（无警告、不阻断）：非叶节点、`type` 为 `None`、`type_spec` 未解析、
delete/remove sentinel（无值可校验）。校验器会吞掉任何意外的 pyang 异常——它绝不
能让构建/解析崩溃。

**identityref 的细节**：identityref 的**命名空间解析**（输出哪个 `xmlns`）在
`build()` 里是**硬错误**——不存在的 identity 无法声明命名空间，故 `build()` 抛
`BuildError`。只有**类型约束**违例（identity 存在但不派生自 leaf 的 base）才是非
阻断警告。

## 反向解析

两个入口，与正向构建器对称：

- `--from-xml` / `parse_reply()`——完整 `<rpc-reply>` 外层。
- `--from-fragment` / `parse_fragment()`——裸数据树元素（如 `<edit-config>` 的
  `<config>` 内容，或剥掉 `<rpc-reply><data>` 外层的 subtree-filter 回包）。

```bash
# 完整 <rpc-reply> -> JSON 外壳（module/root 从 xmlns 推断）
python -m yang_xml_gen.cli reply.xml --from-xml

# 裸数据片段 -> data-only JSON（--from-fragment 默认）
python -m yang_xml_gen.cli config.xml --from-fragment
```

### 回包形态 → JSON（`parse_reply`，RFC 6241）

`parse_reply` 按 `<rpc-reply>` 的第一个子元素派发：

1. **`<data>`**（数据回包）——单根 payload → `{"module", "root", "data"}`（或
   `--data-only` 时只返回 `data`）；多根 payload → 仅 `--data-only` 支持（外壳是
   单根模型，否则抛 `ParseError`，提示用 `--data-only` 或 subtree filter 收窄）。
   空 `<data/>` → `{}`（`--data-only`）或 `ParseError`（外壳：无从推断）。
2. **`<ok/>`** → `{"ok": true}`。
3. **`<rpc-error>`** → `{"rpc-error": [...]}`——每个 error 是其子元素
   （`error-type`、`error-tag`、`error-severity`、`error-message`、`error-path`、
   `error-info`）的结构化字典。这些属 NETCONF base namespace、不被 YANG 建模，故
   走 schema-less 解析。

未知回包形态 → `ParseError`。

### 类型还原

反向类型还原与正向 `_to_str` 对称：

| YANG leaf 类型 | XML 文本 | JSON 值 |
|---|---|---|
| `boolean` | `true` / `1` | `true` |
| `boolean` | `false` / `0` | `false` |
| `empty` | （无 text，存在即语义） | `true` |
| `identityref` | `prefix:ident` | 原样保留带前缀串 |
| 其余（string / enumeration / decimal64 / integer / …） | text | text 字符串 |

正向 builder 一律输出带前缀的 identityref（`toaster:wheat-bread`）；反向原样保留，
故 round-trip 输入需用带前缀形态才能精确相等（裸 ident 反向后会带上前缀）。

### `nc:operation` 往返

正向的 `nc:operation`（即 `_operation: delete` sentinel）在反向对称保留：

- container / list entry 上的 `nc:operation` → entry 里的 `"_operation"` 键。
- leaf 上的 `nc:operation="delete"`（或 `remove`）且无 text → 哨兵
  `{"_operation": "delete"}`。
- leaf 同时有值和 operation（正向工具产不出的形态）→ 只取值，丢弃 operation。

`get`/`get-config` 回包通常**不带** `nc:operation`，故主用例不受影响；此往返主要
服务于「把 edit-config 风格片段反向解析」的边缘场景。

## 读取路径：get / get-config

`wrap: get-config` 读某 datastore 的配置；`wrap: get` 读 running 配置合并运行态
state。用 `filter`（subtree）或 `filter-select`（xpath）收窄回包；两者都省略则全量取。

关键洞察：subtree filter 的「选择子树」内容与 `build()` 已能产出的数据片段**完全
同形**——只填 key 的 list entry 是「内容匹配节点」（选特定条目），空容器是「选择
节点」（选整棵子树）。故 filter 内容直接复用 `build()`，无需新写序列化逻辑。

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "wrap": "get-config",
  "target": "running",
  "message-id": 201,
  "filter": { "toaster": {} }
}
```

### filter 语义（RFC 6241 §6.2/§6.4）

| filter 形态 | spec 键 | 选择语义 |
|---|---|---|
| subtree，key-only entry | `filter: {interface: [{name: eth0}]}` | 内容匹配：选 `name=="eth0"` 的条目 |
| subtree，空容器 | `filter: {interface: [{name: eth0, ipv4: {}}]}` | 选择：选 eth0 下的整个 ipv4 子树 |
| subtree，多 entry | `filter: {interface: [{name: eth0}, {name: eth1}]}` | 多个内容匹配节点，各选一条 |
| xpath | `filter-select: "/if:interfaces/..."` | XPath 1.0 表达式，由设备求值 |
| 无 | （都省略） | 全量取 |

### `<get>` 与 `<get-config>`

- `<get-config>`（§7.5）：读指定 datastore（`<target>`），只返回配置数据。用
  `target` 键（默认 `running`）。
- `<get>`（§7.7）：无 `<target>`，返回 running 配置合并运行态 state。读 operational
  state 用这个。

### `<with-defaults>`（RFC 6243）

NETCONF 默认不回传节点的 schema 默认值（只回传显式设置过的节点）。RFC 6243 的
`<with-defaults>` 参数控制此行为；它由 `ietf-netconf-with-defaults` augment 进
`<get>`/`<get-config>` 的 input，故元素落在该模块自己的 namespace（不是 NETCONF
base namespace）。`with-defaults` 键取值为四种模式之一：

| 模式 | 语义（RFC 6243 §3） |
|---|---|
| `report-all` | 回传所有节点，包括未显式设置的默认值 |
| `report-all-tagged` | 同上，但默认值节点带标记 |
| `trim` | 不回传值等于 schema 默认值的节点 |
| `explicit` | 只回传显式设置过的节点（NETCONF 默认行为，显式声明用） |

`<with-defaults>` 作为 `<get>`/`<get-config>` 的**最后一个**子元素（在 `<target>`
和 `<filter>` 之后），与 augment 在模型里的位置一致。非法模式在 wrapper 层抛
`ValueError`——不会产出报文。

## 打包发布

[`pyproject.toml`](pyproject.toml) 用 setuptools 后端。

```bash
python -m pip install -e .     # editable（开发期）
python -m pip install .        # 普通 wheel 安装
```

安装后得到 `yang-xml-gen` 命令（`[project.scripts]` → `yang_xml_gen.cli:main`），
等价于 `python -m yang_xml_gen.cli`。

| 字段 | 取值 |
|---|---|
| 项目名 | `yang-xml-gen` |
| 版本 | `0.7.0` |
| `requires-python` | `>=3.10`（用 PEP 604 `X \| Y` 联合类型语法） |
| 依赖 | `pyang>=2.5`、`PyYAML>=6.0` |
| 入口 | `yang-xml-gen = "yang_xml_gen.cli:main"` |
| 类型标记 | `py.typed`（PEP 561）随 wheel 发布 |
| 许可证 | MIT，经 `project.license = {file = "LICENSE"}` 声明 |

**不打包 models**——123 个 `.yang` 体积大且为上游产物（按
[`models/README.md`](models/README.md) 自行获取）。代价是 pip 安装后须传
`--models-dir` 或 `YANG_XML_GEN_MODELS_DIR`，否则抛 `RuntimeError`——让「缺模型」
显式报错而非静默空跑。仓库内 editable 安装仍能自动发现 `models/`。

## 测试

```bash
python -m pytest -q     # 172 项测试
```

| 测试模块 | 覆盖 |
|---|---|
| `tests/test_generator.py` | 正向构建：命名空间、key 顺序、identityref 前缀、boolean、operation 注入、错误处理 |
| `tests/test_delete.py` | `_operation: delete` 作用于 list entry / container / leaf；多 entry；`delete` vs `remove` |
| `tests/test_scaffold.py` | `--template` 骨架：list 占位、state 过滤、leaf-list、empty/decimal64/union leaf |
| `tests/test_choice_rpc.py` | choice/case 扁平化、rpc input 序列化、augment namespace |
| `tests/test_filter.py` | get / get-config、subtree + xpath filter、`<with-defaults>`、CLI 端到端 |
| `tests/test_parse.py` | `parse_reply` / `parse_fragment`、回包形态、类型还原、`nc:operation` 往返、CLI `--from-xml`/`--from-fragment`、输入 BOM/UTF-16 解码 |
| `tests/test_validator.py` | 每种约束的 `validate_value`、`emit_warnings`、严格模式、builder/parser 集成 |
| `tests/test_packaging.py` | `pyproject.toml` 字段、文件存在性、models 不打包、`cli.main` 可调用 |

## 已知限制

明确不做（设计取舍）：

- **不做 schema 级校验**：`when` / `must` / `mandatory` / `min-elements` / choice
  互斥不查。校验只覆盖单个叶值对其类型的约束。
- **校验永不阻断**：用 `filterwarnings("error", category=YangValidationWarning)`
  升级为硬错误。
- **不支持 `action` / `notification`**（action 嵌套在 container/list 下需目标路径；
  notification 是设备→管理者方向）。
- **不生成 rpc output**（只生成 rpc 调用 / input 方向）。
- **wheel 不打包 models**；不做 `fetch_models.py` / CI 自动获取。
- **typedef 链不递归**到根类型。
- **leaf-list 单值删除**（`<leaf nc:operation="delete">value</leaf>`）不支持——与
  leaf 的无值删除形态冲突。整棵 leaf-list 删除用父容器的 `_operation: delete`。
- **结构化 `anyxml`** 按文本 leaf 处理，不解析 XML 子树。

## 项目结构

```
yang-xml-gen/
├── models/                 # YANG 模型（不打包进 wheel；见 models/README.md）
│   ├── README.md           #   如何获取 123 个上游 IETF/OpenConfig 模型
│   └── example-toaster@2026-07-17.yang   # 提交进仓库的示例模型，本 README 用
├── scripts/
│   └── compile_models.py   # 批量编译 + 一致性校验（退出码可接入 CI）
├── src/yang_xml_gen/
│   ├── loader.py           # 加载全部模型；identity/namespace 索引；models 目录解析
│   ├── schema.py           # pyang 语句 -> SchemaNode 树（TypeInfo 含 type_spec）
│   ├── scaffold.py         # 由 schema 生成空白 JSON 模板
│   ├── xml_builder.py      # 数据+schema→XML（正向，非阻断校验）
│   ├── xml_parser.py       # <rpc-reply>/裸片段 → JSON（反向，非阻断校验）
│   ├── validator.py        # YANG 类型约束校验（range/length/pattern/enum/...）
│   ├── wrappers.py         # 裸片段 / edit-config / rpc / get-config / get 报文封装
│   ├── cli.py              # CLI 入口（--from-xml / --from-fragment / --template / ...）
│   ├── __main__.py         # 支持 python -m yang_xml_gen
│   └── py.typed            # PEP 561 类型标记
├── tests/                  # test_generator / delete / scaffold / choice_rpc / filter / parse / validator / packaging
├── examples/               # YAML/JSON 输入 + 模板/填值/rpc/get/delete + 回包样例
├── pyproject.toml          # 打包配置（setuptools，entry point: yang-xml-gen）
├── LICENSE                 # MIT
├── README.md               # 英文
└── README_zh.md            # 中文（本文件）
```

## 许可证

MIT——见 [LICENSE](LICENSE)。Copyright (c) 2026 qinjh.
