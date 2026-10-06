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

仅支持UTF-8有限JSON、GET/POST。无WebSocket/SSE/文件流/HLS、认证header注入、每用户session状态机或CORS开发服务器。显式origin allowlist不是DNS-rebinding/SSRF安全沙箱：仅对可信URL使用，不暴露给远程提交任意URL的用户。HTTP回放是本地开发服务，不是生产服务器，连接并发/内存资源需OS隔离。

## v0.3 顺序场景：测试重试、恢复与多步骤客户端

固定 `replay` 模式仍按请求返回同一fixture。新 `scenario` 模式使用一个**全局共享**顺序，适合单个受控客户端测试，不是每用户独立session：

```sh
python examples/retry_scenario.py
contractdock scenario scenario.json --port 8099
```

`scenario.json` 是显式读取的JSON，无自动网络录制或工程策略加载：

```json
{
  "version": 1,
  "steps": [
    {"fixture": "此处放完整且校验通过的503 fixture对象，非文件路径", "repeat": 2, "delay_ms": 100},
    {"fixture": "此处放相同请求的完整200 fixture对象", "repeat": 1}
  ]
}
```

上面仅展示结构，字符串占位符不可执行；示例脚本生成完整可验证描述。Python API为 `scenario_server(description, port=0)`，与 `replay_server` 一样返回需关闭的本地HTTP server。每步只能包含 `fixture` 和可选 `repeat` / `delay_ms`；1至100步，累计不超过10,000响应，repeat严格整数1至10,000，delay_ms严格整数0至5,000（bool拒绝），完整描述最多1MiB。启动前验证所有fixture的校验/脱敏/schema，并复制内部状态，外部字典后续变动不会改变服务响应。

仅匹配当前步骤的规范method/path/query/脱敏body才能消耗一次repeat，重复次数用尽后推进；错序或未知请求返回409，非法JSON/长度头返回400，都不推进。全步骤结束返回410，不自动循环、不回退网络。多个并发请求按服务器锁内**匹配预留顺序**消耗；响应实际到达可因延迟乱序。匹配后即消耗，即使客户端断开；延迟在锁外执行，不阻塞后续步骤。服务重启从头开始，无持久场景状态；延迟是调度请求，不是硬实时精度保证。

示例完全使用合成脱敏fixtures，实际通过本地HTTP请求观察 `[503,503,200]` 重试恢复、错序409和结束410。适合可重复测试“暂时不可用→恢复”，不意味着真实后端故障统计或生产负载模型。现有固定回放同样复制已验证fixture，避免调用者在启动后改响应绕过完整性检查。

## v0.3.1 HTTP完整性与故障验收

录制拒绝重复、非数字、负值、超界Content-Length，拒绝不支持的Transfer-Encoding（仅单个chunked）及同时携带TE/长度的歧义响应。声明长度与实际读取bytes不一致即失败；chunk截断/HTTP协议错误转为不含URL、query或响应内容的CLI退出码2/complete:false，不发布fixture。正常完整chunked、确切长度及无长度以连接关闭结尾的JSON保持支持；没有长度的响应无法证明服务端原本打算发送更多数据。

离线回放/场景在匹配前拒绝所有Transfer-Encoding头（包括空值）、GET非零body及绝对URL请求目标；畸形输入400，不消耗步骤。场景匹配成功后断连仍按既有规则消耗，不自动回退。固定回放也使用同一传输验证。

新增9项真实loopback/合成发布故障测试，总45tests：先在旧实现复现短JSON误录、chunk错误逃逸与场景提前消耗，再验证合法framing、fsync失败无输出、并发发布不覆盖完整竞争fixture。CI在Windows/Linux安装wheel后从源码外重复全部9项故障验收。仍是可信URL的本地开发工具，不是完整HTTP代理、请求走私/SSRF防御产品或生产服务器；不保证全程绝对超时、恶意流量资源隔离和断电目录持久性。

可复现回放/场景并发基准：`python -m benchmarks.replay --requests 256 --workers 4`。它校验所有HTTP响应及场景响应数量，不将错误请求计为吞吐；实测与冷启动/顺序/内存测量限制见 [方法说明](benchmarks/README.md)。不是生产负载或场景比固定回放更快的证明。

## 后续里程碑

1. OpenAPI导入、optional/union schema和请求响应双向兼容定义。
2. 可配置字段脱敏、fixture隐私审计和审批。
3. 显式本地代理录制，安全受控header输入（绝不落盘）。
4. 在全局顺序场景基础上增加显式每用户session、分支/分页和受控断连模拟。
5. 大型fixture、并发/断连测量、桌面审阅和签名发布。

新作品集工程，不宣称符合飞书活动的原有私有仓库准入。
