# 项目补充说明

## 本地分析输入

- 项目目录：`E:\qqspeed-replay-analyzer`。
- 录像目录：`E:\qqspeed-replay-analyzer\游戏录像`，存放用于本地分析的 QQ飞车 `.sav` 录像。
- 游戏安装目录：`E:\Games\WeGameApps\QQ飞车`，可只读访问客户端程序与资源文件。

## 已有逆向工具

- IDA Professional 9.1：`C:\Program Files\IDA Professional 9.1`。
- 命令行入口：`C:\Program Files\IDA Professional 9.1\idat.exe`；优先使用该入口进行静态批处理分析。
- 图形界面入口：`C:\Program Files\IDA Professional 9.1\ida.exe`。
- 使用 IDA 时遵守下述分析约束，不启动或调试运行游戏客户端。

## 分析约束

- **禁止打开或启动游戏**，包括通过 WeGame、游戏启动器、客户端可执行文件或其他工具间接启动游戏。
- 允许通过静态逆向工程解析游戏逻辑，包括反编译、反汇编，以及分析客户端文件、资源和录像格式；分析过程不得执行游戏客户端。
- 保留原始录像和游戏安装文件；分析结果、提取文件及临时产物写入项目目录内。
- 原始录像、游戏客户端资源及本机配置仅用于本地分析，不随源码发布。

## 过程记录

- 过程记录统一保存到项目根目录的 `.temp/`（`E:\qqspeed-replay-analyzer\.temp`）。
- 分析笔记、执行命令及结果、验证记录，以及 IDA 数据库、日志、临时脚本和中间导出文件均放入该目录。
- `.temp/` 仅用于本地工作，加入 Git 忽略规则，不随源码发布。

## 代码定位

- 如果仓库根目录存在 `.codegraph/`，理解或定位代码时先使用 CodeGraph（`codegraph_explore` 或 `codegraph explore`）。
- 如果不存在 `.codegraph/`，跳过 CodeGraph，不自行建立索引。
- 优先使用命令行工具完成分析，缺少必要工具时自行安装后使用。
