# YANG 模型目录

本目录存放工具依赖的 **123 个 YANG 模型**（按模块名约 101 个，部分模块
保留多个 revision）。这些模型**不纳入版本控制**（见仓库根 `.gitignore`），
克隆后需自行获取并放入本目录，工具才能运行。

## 模型构成

| 来源家族 | 数量 | 示例 |
|---|---|---|
| IETF 标准模块 | 34 | `ietf-interfaces`、`ietf-netconf`、`ietf-ip`、`ietf-yang-types` |
| IANA 维护模块 | 7 | `iana-if-type`、`iana-crypt-hash` |
| OpenConfig 模块 | 74 | `openconfig-interfaces`、`openconfig-system`、`openconfig-aaa` |
| sysrepo / libnetconf / 通用 | 8 | `sysrepo`、`libnetconf2-netconf-server`、`notifications`、`yang` |

### 例外：`example-toaster@2026-07-17.yang`

`example-toaster@2026-07-17.yang` 是一个**例外**——它不是上游模型，而是为本项目
README 编写的示例模块（扩展自经典的 netconfcentral toaster），用于演示 string /
uint8 / enumeration / identityref 等叶类型在配置生成中的处理。它**纳入版本控制**
（仓库根 `.gitignore` 的 `models/*.yang` 规则对它用 `git add -f` 强制跟踪），其余
上游模型仍按上文「不纳入版本控制」处理。

## 获取方式

模型文件名带 `@<revision-date>.yang` 后缀，可从以下官方来源获取：

### 1. IETF / IANA 模块

- **IETF YANG Catalog**：<https://www.yangcatalog.org/>
  （按模块名搜索，下载指定 revision 的 `.yang` 文件）
- **IETF GitLab（YANG 参数归档）**：
  <https://gitlab.com/ietf-interfaces/yang-parameters>
- **各 RFC 的官方附录**：如 `ietf-interfaces` 对应 RFC 8343，
  `ietf-netconf` 对应 RFC 6241。

### 2. OpenConfig 模块

- **OpenConfig 公开仓库**：<https://github.com/openconfig/public>
  （`release/models/` 目录下按子系统分目录存放）

### 3. sysrepo / libnetconf

- **sysrepo 仓库**：<https://github.com/sysrepo/sysrepo>
- **libnetconf2**：<https://github.com/CESNET/libnetconf2>

## 目录布局要求

把所有 `.yang` 文件**直接平铺**在本目录下（不要按来源建子目录），文件名
保留 `module@revision.yang` 格式，例如：

```
models/
├── ietf-interfaces@2018-02-20.yang
├── ietf-netconf@2013-09-29.yang
├── openconfig-system@2026-06-11.yang
├── openconfig-aaa@2025-01-02.yang
└── ... （共 123 个）
```

工具的 `Loader` 把本目录作为 pyang 的搜索路径（`--dir`），所有 `import`
链在此解析；平铺布局与 `scripts/compile_models.py` 的扫描方式一致。

## 校验安装是否完整

放好模型后，运行第 1 步的编译脚本确认 0 error：

```bash
python scripts/compile_models.py
```

输出末尾应出现类似 `compiled N modules, 0 errors` 的成功信息。若报
`module not found`，说明缺模型——按报错的模块名从上述来源补齐即可。

## 为什么不纳入版本控制

- 模型总体积 2.3M、123 个文件，且为上游项目产物——重复存储意义不大。
- 各上游有独立的版本与发布节奏，纳入后难以同步更新。
- 许可证归属上游项目（IETF / OpenConfig / sysrepo 各自有 license），
  混入本工具仓库不合适。

如需把模型一起纳入仓库（例如离线分发场景），删除仓库根 `.gitignore` 中的
`models/` 一行，再 `git add models/` 即可。
