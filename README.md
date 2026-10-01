# 分类改革申诉复核

本项目维护分类改革申诉复核的领域约定、角色边界与样例数据，并提供一个零外部依赖（Python 标准库 + SQLite）的完整后端服务，供接口和自动化验证统一使用。

契约覆盖申诉院校、复核秘书、独立专家、审计管理员四个角色，明确**时限控制、利益回避、决定差异、幂等提交**四项关键不变量。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/appeal_review/`：申诉复核后端（存储层、领域服务、HTTP 接口）。
  - `store.py`：SQLite 表结构、`BEGIN IMMEDIATE` 事务与审计写入。
  - `service.py`：领域规则（期限、版本、回避、签署、暂缓、合并/撤回/重开、隔离查询）。
  - `api.py` / `__main__.py`：标准库 HTTP 服务与命令行入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域服务规则测试与端到端 HTTP 测试（共 32 项）。

## 启动

```bash
PYTHONPATH=src python3 -m appeal_review --db appeal_review.db --port 8080
# 可选：--supplement-days 10 --required-signatures 3
```

预置账号（角色）：

| 用户名 | 角色 |
| --- | --- |
| `school_a` / `school_b` | 申诉院校（甲校 / 乙校） |
| `sec` | 复核秘书 |
| `expert1`–`expert4`、`orig`（原评审人） | 独立专家 |
| `admin` | 审计管理员 |

## 接口约定

- 登录：`POST /api/auth/login`，body `{"username": "..."}`，返回 Bearer 令牌。
- 鉴权：所有业务接口需要 `Authorization: Bearer <token>`。
- **安全重试**：写请求携带 `Idempotency-Key: <键>`。同主体同键重放返回首次结果；同键搭配不同请求体返回 `409 IDEMPOTENCY_KEY_REUSED`。

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /api/cases` | 院校 | 提交申诉 |
| `POST /api/cases/{号}/accept` | 秘书 | 受理（仅「提交」态） |
| `POST /api/cases/{号}/supplement` | 秘书 | 要求补证，设定受理期限 |
| `POST /api/cases/{号}/evidence` | 院校 | 提交证据；**逾期材料只作为新版本留存**，`on_time=false`，不覆盖旧版本 |
| `POST /api/cases/{号}/recusals` | 秘书/专家 | 登记回避；原评审人登记后自动移出复核名单且无法被指派/签署 |
| `POST /api/cases/{号}/reviewers` | 秘书 | 指派无回避关系的专家 |
| `POST /api/cases/{号}/decision` | 秘书 | 作出决定；签署人须在案且无回避，**不足法定人数返回 `409 QUORUM_NOT_MET`** |
| `POST /api/cases/{号}/sign` | 专家 | 追加签署 |
| `POST /api/cases/{号}/stays` / `DELETE …/stays` | 秘书 | 暂缓执行措施的采取与解除 |
| `POST /api/merges` | 秘书 | 同院校案件合并，证据、期限、名单并入并重新编号版本 |
| `POST /api/cases/{号}/withdraw` | 院校/秘书 | 撤回（院校只能撤回本案） |
| `POST /api/cases/{号}/reopen` | **仅管理员** | 撤回/合并后重开，全程留痕 |
| `GET /api/cases[/{号}]` | 按角色 | 院校只见本校案件（他校返回 404，不暴露存在），专家只见在案案件 |
| `GET /api/decisions/own` | 院校 | 本校决定及 `differs_from_original` 差异标记 |
| `GET /api/audit` | **仅管理员** | 全过程动作日志；不含证据正文或材料指针 |

## 不变量如何落地

1. **时限控制**：补证期限落在 `deadlines` 表；提交证据时与截止时间比对，逾期材料强制进入下一证据版本并标注 `late_reason`，邮件往来的时间争议由服务端时间戳定论。
2. **利益回避**：`recusals` 与复核名单互斥；登记回避即移出名单，指派和签署两道关卡都拒绝有回避关系者，确保原评审人退出复核。
3. **决定差异**：`modify`/`revoke` 自动标记 `differs_from_original` 并保留原结果摘要，院校在 `/api/decisions/own` 看到本校决定差异。
4. **幂等提交**：所有写操作在 `BEGIN IMMEDIATE` 事务内执行，幂等键唯一绑定（主体、键、请求体哈希、首次响应快照），网络重试不会产生重复写入。

## 验证

```bash
python3 -m unittest discover -s tests -v   # 契约 + 32 项后端回归
python3 -m compileall -q src tools tests   # 编译检查
python3 tools/check_contract.py domain/contract.json
```
