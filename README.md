# 科研公示复杂表格·结构化采集

面向合作资料接收人员的 Python 采集组件。解析固定单层设备公示表，精确转换人民币金额并保存原页。
输入样本都是本地合成数据，example 域名只表示来源身份，不需联网访问。

## 安装与运行

使用 Python 3.11，依赖仅为标准库，SQLite 随解释器提供，无需安装第三方包。
从项目根目录执行；演示启动真实的回环 HTTP 来源、处理响应、事务入库后读取实际结果。
每次演示使用独立临时目录，结束后清理；通常两秒内结束，不回放历史输出。

```bash
python3 -m compileall -q collector
python3 -m unittest discover -s tests -v
python3 -m collector demo
```

常规导入保留在本地数据库中，可重复运行；默认路径已忽略，不会进入版本库。

```bash
python3 -m collector import --input fixtures/sample.json --db var/records.sqlite3
python3 -m collector show --db var/records.sqlite3
python3 -m collector serve --db var/records.sqlite3 --port 8080
```

公示网页（HTML 附件）走语义层：先重建逻辑网格、识别语义列，再决定是否落正式目录。

```bash
python3 -m collector import --input fixtures/equipment.html --columns fixtures/columns.json --db var/records.sqlite3
python3 -m collector explain --input fixtures/equipment.html --columns fixtures/columns.json
python3 -m collector catalog --db var/records.sqlite3
python3 -m collector quarantine --db var/records.sqlite3
python3 -m collector replay --db var/records.sqlite3 --columns fixtures/columns.json
```

`import` 成功发布返回 0；页面被隔离时原文已留存并记录原因，命令返回 1。
`explain` 只解释不写库，供业务人员核对组合表头、重复表头与单位换算的解释结果。
`replay` 用当前规则重放仍处于隔离状态的页面；同一页面在同一规则版本下只产生一个批次。

查询服务监听回环地址，`GET /records` 返回数据库中的记录；按 Ctrl-C 停止。
业务核对入口：`GET /catalog` 正式目录当前视图，`GET /pages` 全部页面与批次状态，
`GET /quarantine` 仍隔离的页面及原因，`GET /page/<digest>` 单页全部批次的映射与原因。

## 组件与数据

`collector/domain.py` 负责来源格式和领域校验；`storage.py` 保留原始对象、导入凭据和当前记录。
单次导入在同一事务内提交，异常时整体回滚；相同来源和原文只登记一次凭据。
当前记录以领域标识为键，同键再次导入替换当前观察值，不代表保留了完整修订历史。
`transport.py` 提供请求客户端和本地响应服务；服务按路径依次消费响应脚本，并记录实际请求。
`api.py` 提供只读查询入口。`fixtures/sample.json` 是当前导入器支持的输入。
其他附件描述同领域的合成页面、协议或事件，可以由调用方构造本地来源。
当前响应服务是顺序脚本工具，不自动推断认证、文件或表单业务；领域对象提供对应的本地交互。

语义层由 `collector/grid.py` 与 `collector/semantics.py` 组成，处理 `fixtures/equipment.html`
这类公示网页：先把 rowspan/colspan 展开为可追溯到原单元格的逻辑网格（隐藏列、`<sup>` 脚注、
重叠合并都在这一层识别），再按 `fixtures/columns.json` 的必需列与单位白名单做语义映射。
数量与金额使用不同的单位、千位分隔与脚注规则；金额按表头单位统一换算为元并保留原值。
跨行取值只来自真实的合并覆盖，业务主键为机构+编号，绝不对全表空白向下填充。
不认识的必需列、单位冲突、重叠合并单元格等情况整页隔离：原页留存、原因入库、不挪列猜测。
已发布记录按（主键, 规则版本）保留原始坐标引用；规则升级后重放隔离页面生成新批次，
并发重放由事务与唯一约束保证不产生重复正式批次。

## 可复现运行

宿主机或容器均不需要网络。镜像准备好以后可以关闭外部网络再运行上述命令。
仓库内不含真实账号、新闻正文或人员资料；不要将生产凭据写入样本或日志。
