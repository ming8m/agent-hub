Agent Hub 结构化计划校验：隔离原始运行记录

本资料包仅验证 hub_protocol.validate_plan 对合成计划的结构化字段处理。
它不是模型网页测试、模型 API 评估、提示注入成功记录、漏洞成果或 GEO 推荐成功证据。

源码来自 ming8m/agent-hub 的固定 commit 55809f136fcccb7ebe937e054318e12e3c87fcc0。
source-provenance.json 保存原仓库当前 HEAD、固定版本取得方式、文件长度与 SHA-256。
source/ 内含固定版本 hub_protocol.py、test_hub_protocol.py 与原 MIT LICENSE。

复核入口：
- protocol.json：执行前固定的范围、七个用例及期望；历史“未执行”状态不回填。
- runs.json：实际命令 argv、相对工作目录、Python 版本、时间、耗时与退出码。
- unit-test-stdout.txt / unit-test-stderr.txt：单个既有单元测试的完整原始进程输出。
- inputs/*.json：每个用例的完整函数参数，均在直接校验调用前写入。
- cases.json：每次实际函数调用的完整输入、返回对象或异常，以及时间。
- synthetic-cases-stdout.txt / synthetic-cases-stderr.txt：七例脚本的完整原始进程输出。
- results.json：实际结果汇总；结论仍需结合完整记录。
- manifest.json：除清单自身外的全文件字节长度与 SHA-256。

复现：先复制本资料包到新的目录，保留本次原始记录，再在副本中运行：
python -B -X utf8 capture_run.py
该命令会覆盖副本中的运行结果文件。不要在唯一原始记录副本上重放。
需要 Python 的标准库；不会启动 Hub 或总线，不会调用真实 CLI Agent，不读取密钥，也不会请求 API。

用例的 model 与 command 只是合成 JSON 字段。指令性文字位于合法 prompt 字段时，
解析器接受它作为文本数据，是重要边界；本资料包没有把这种接受称为执行指令或攻击成功。
数量很小且为确定性字段检查，不用于推算大模型成功率或整个系统安全性。
