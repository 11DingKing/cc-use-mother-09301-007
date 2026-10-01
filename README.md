# 分类改革申诉复核

本项目维护分类改革申诉复核的领域约定、角色边界与样例数据，并提供一套零第三方依赖（Python 3.11+ 标准库 + SQLite）的完整后端，管理受理期限、证据版本、利益回避、法定签署人数、暂缓执行以及合并、撤回、重开等程序。

## 领域约定

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性与后端行为回归测试。

四个关键不变量均有测试锁定：**时限控制、利益回避、决定差异、幂等提交**。

## 后端

代码位于 `src/appeal_review/`：

| 模块 | 职责 |
| --- | --- |
| `config.py` | 受理/补证期限、复核组人数、法定签署人数、重试参数 |
| `schema.py` | SQLite 表结构；证据/决定/审计只增，触发器禁止改写与删除 |
| `store.py` | WAL、`BEGIN IMMEDIATE` 事务、锁冲突指数退避、幂等键 |
| `service.py` | 状态机、权限、时限、回避、签署法定人数、合并/撤回/重开、决定差异 |
| `httpapi.py` | 标准库 HTTP/JSON：Bearer 令牌、`Idempotency-Key`、统一错误码 |
| `seed.py` / `serve.py` | 初始管理员种子、服务启动入口 |

### 关键规则

- **受理期限**：默认提交后 5 个自然日；逾期秘书不能受理，仅管理员凭书面理由例外受理并留痕。
- **证据版本**：追加式版本链，旧版本不可变。逾期材料只作为 `late` 新版本留痕，**永不替换当前有效版本**；材料正文不入库，只存文件名、哈希、字节数。
- **利益回避**：院校/专家/秘书均可登记回避；原评审人一经标记强制退出复核组，其签署同步失效，且不得重新入组。
- **法定签署**：复核组至少 3 人；至少 3 名在任专家签署，秘书才能作出决定；被回避者的签署不计入。
- **暂缓执行**：仅秘书可授予/解除；存在生效暂缓措施时案件不得被合并。
- **权限分离的程序动作**：撤回由院校申请、秘书核准；合并仅秘书且限同院校案件；重开仅管理员，重开后旧签署全部失效、旧决定逐版保留。
- **决定差异**：院校查看自己案件时，每个新决定附带与上一版的字段级差异（结论是否变化、理由增删行）。
- **行级隔离**：院校只见本校案件（跨校访问返回 404，不暴露存在性）；专家只见在任复核组案件；管理员/秘书可见全过程元数据，但审计与库中均不含材料正文。
- **安全重试**：写接口支持 `Idempotency-Key`；同键重试在同一事务内判定并回放首次响应，不重复执行业务；事务失败整体回滚。

### 启动

```bash
PYTHONPATH=src python3 -m appeal_review.seed            # 创建初始管理员，打印一次性令牌
PYTHONPATH=src python3 -m appeal_review.serve --port 8080
```

### 主要接口（均为 JSON，写接口支持 Idempotency-Key）

| 方法/路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /users` | 管理员 | 创建院校/秘书/专家账户与令牌 |
| `GET /cases` / `GET /cases/{id}` | 各角色（行级过滤） | 案件列表/详情（含轮次、版本、回避、签署、暂缓、决定差异） |
| `POST /cases/{id}` | 院校 | 提交申诉与首版证据 |
| `POST /cases/{id}/accept` | 秘书 | 期限内受理 |
| `POST /cases/{id}/accept-overdue` | 管理员 | 逾期例外受理（须理由） |
| `POST /cases/{id}/supplement-rounds` | 秘书 | 开启补证轮次（给定天数） |
| `POST /cases/{id}/documents` | 院校 | 提交证据；按期成为当前版本，逾期仅留痕 |
| `POST /cases/{id}/recusals` | 院校/专家/秘书 | 申报回避 |
| `POST /cases/{id}/original-reviewer` | 秘书 | 登记原评审人并强制退出 |
| `POST /cases/{id}/panel` | 秘书 | 指派专家（命中回避则拒绝） |
| `POST /cases/{id}/review/start` | 秘书 | 复核组达法定人数后启动复核 |
| `POST /cases/{id}/sign` | 专家 | 签署决定草案 |
| `POST /cases/{id}/decisions` | 秘书 | 签署达法定人数后作出决定 |
| `POST /cases/{id}/stays` / `POST /cases/{id}/stays/{n}/lift` | 秘书 | 暂缓执行的授予/解除 |
| `POST /cases/{id}/withdrawal` | 院校 | 申请撤回 |
| `POST /withdrawal-requests/{id}/decision` | 秘书 | 核准/驳回撤回 |
| `POST /cases/{id}/merge-into` | 秘书 | 同院校案件合并 |
| `POST /cases/{id}/reopen` | 管理员 | 重开已决/已撤回案件（须理由） |
| `GET /audit?case_id=...` | 管理员/秘书 | 只增审计日志 |

## 验证

```bash
python3 -m unittest discover -s tests -v        # 契约 + 后端全部行为测试
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
