# ContractDock

后端服务不可用时，也能用经过脱敏的JSON样例离线开发客户端；API升级后对照旧响应发现字段消失、类型或状态码变化。**真实录制→关闭原服务→离线回放**在测试/demo中验证，不靠把请求转发回原服务伪装离线。

Python 3.12+，无运行时第三方依赖。

```sh
python examples/demo.py
python -m contractdock record http://127.0.0.1:9000/players player.json --allow-origin http://127.0.0.1:9000
python -m contractdock replay player.json --port 8099
python -m contractdock compare player-old.json player-new.json
python -m unittest discover -s tests -v
python -m pip install .
contractdock --help
```

`record --method POST --body-file request.json`录制显式POST。网络操作必须明确允许origin（协议、host、port），默认不允许任何地址；不跟随重定向，不使用系统代理，不保存请求/响应headers。录制是一次明确网络请求，不是浏览器透明代理，不会自动拦截其他程序流量。

回放只绑定 `127.0.0.1`，固定fixture集只读，未知请求404、畸形请求400、过大请求413，**不会fallback到网络**。按method+路径+排序query+脱敏body匹配；敏感query/body不同值归一到同一匹配。这不能模拟身份权限差异，需自行分开fixture集/服务端口。重复请求key拒绝启动，避免随机选择。

退出码0录制成功/比较无已观察漂移，1比较有漂移，2无完整结果。回放常驻直到Ctrl+C。fixture包含请求key、脱敏JSON响应、观察类型树和SHA256；已有文件不覆盖，以同目录hard link原子发布（文件系统需支持）。1MiB请求/响应/完整fixture、100query参数、64JSON层级限额。

## 脱敏与比较边界

默认匹配字段名中的password/passwd/secret/token/authorization/cookie/api-key/credential，递归将其值替换为`[REDACTED]`。headers全不落盘。**不能识别所有秘密/PII**：放在URL路径、普通字段、字符串嵌套JSON或不常见字段名的数据可能保留。录制前审查API，分享fixture前再次审查；不以默认脱敏保证可公开。哈希校验只防意外篡改，不是签名。

比较是单样例的观察schema漂移检查，不是OpenAPI完整兼容性证明。新增对象字段可通过，旧字段消失/类型变化/状态变化会失败；数组从无元素样例无法推断类型，多对象变体也不等同完整schema；值范围、枚举、业务语义、optional字段、认证、分页和时序不被证明。样例不同本身可能产生误报，应挑选同请求同场景样例。

v0.2对数组的每个新观察形状检查至少一个兼容的旧形状，不能因旧数组有多个object类型而跳过字段验证。空数组对非空数组会输出`uncertainties`和`compatible: null`，不能把没有证据当成兼容证明；`passed`只表示没有已发现的漂移，`compatible`也只针对本次观察样例，仍不是整个API的保证。

仅支持UTF-8有限JSON、GET/POST。无WebSocket/SSE/文件流/HLS、认证header注入、session状态机或CORS开发服务器。显式origin allowlist不是DNS-rebinding/SSRF安全沙箱：仅对可信URL使用，不暴露给远程提交任意URL的用户。HTTP回放是本地开发服务，不是生产服务器，连接并发/内存资源需OS隔离。

## 后续里程碑

1. OpenAPI导入、optional/union schema和请求响应双向兼容定义。
2. 可配置字段脱敏、fixture隐私审计和审批。
3. 显式本地代理录制，安全受控header输入（绝不落盘）。
4. 多步骤session状态机、分页/故障/延时模拟和确定性匹配。
5. 大型fixture、并发/断连测量、桌面审阅和签名发布。

新作品集工程，不宣称符合飞书活动的原有私有仓库准入。
