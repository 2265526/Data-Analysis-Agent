# 数据级权限(Data-Level Permission)设计

> 实现时间: 2026-08 · 触发: 权限审查后用户要求补齐"数据级权限"

## 一、背景与目标

原系统只有**角色级权限**(`user` / `approver` / `admin` 控制接口与页面),底层业务数据对所有角色一视同仁,仅靠"敏感表强制人工审批"(`find_sensitive_tables`, 命中 `customers` 或敏感词表名)做粗粒度闸门。

目标: 在 **AI 智能体生成 SQL → 校验 → 执行** 链路上补充 **表级 / 列级 / 行级** 数据访问控制,且不破坏现有"默认允许"的平滑体验。

## 二、业界调研结论(落地方案依据)

| 来源 | 做法 | 本项目借鉴 |
| --- | --- | --- |
| Apache Ranger | RLEF(行过滤=查询追加 WHERE)/ CLM(列掩码=替换投影列), 插件在执行前改写 SQL | 同款**查询改写**思路 |
| Superset RLS | 按 角色+表 配置 WHERE 子句, 存元数据库 | 行过滤存策略表 |
| Metabase 沙箱 | 用户属性拼过滤条件; **官方明确: 不解析 SQL 就无法细粒度授权**, 只能整库禁 native 查询 | AI 场景必须 AST 解析(AI 生成的 SQL 无法用固定视图覆盖) |
| Metabase 默认策略 | **先 Block 全员再授权**(默认拒绝) | 本项目选"默认允许 + 显式收紧"(历史行为兼容) |

**关键原则(代码注释亦有):**
- 不信任 LLM 守规矩: 权限由确定性代码在 SQL 提交执行前强制, 提示词约束不算权限
- 宁可拒绝不泄漏: 无法安全改写(解析失败 / 归属不明 / 展开失败)一律拒绝
- 单一强制点: 所有 SQL 入口(agent 流水线 + 下钻)过同一 `apply_data_policy`

## 三、数据模型

`data_policy_rules` 表(ORM: `src/models/data_policy.py`, 启动 `create_all` 幂等建表):

| 字段 | 说明 |
| --- | --- |
| `target_type` | `role` \| `user`(用户级优先于角色级) |
| `target_name` | 角色名(`user`/`approver`/`admin`…)或用户名 |
| `table_name` | 业务表(如 `orders` / `customers`) |
| `row_filter` | 行级过滤 WHERE 表达式片段(管理员受信 SQL, 注入查询) |
| `col_access` | JSON: `{"列名": "allow|mask|deny"}` |
| `mask_expression` | mask 列脱敏表达式(PG 方言, 缺省 `'***'`) |
| `enabled` | 软开关(关闭=该规则不生效=默认允许) |

唯一约束: `(target_type, target_name, table_name)`。

## 四、策略语义(核心)

**默认允许 + 显式收紧**: 无任何规则命中的表 = 放行(历史行为不变)。

规则是"限制性声明", 合并规则:
1. **命中范围**: 用户本身(`user` 规则) + 用户所有角色(`role` 规则)
2. **用户级覆盖角色级**: 同一表存在用户规则时, 只看用户规则
3. **多角色命中同一表**:
   - **行过滤**: 所有命中规则 `AND`(交集, 最严格——所有角色都要求满足)
   - **列访问**: 逐列取**最宽松**(任一角色 `allow` 即 `allow`; 否则任一 `mask` 即 `mask`; 否则 `deny`)——保证高权限角色可豁免

**列模式行为**:
- `deny`: 查询显式引用该列 → **整条拒绝**(不泄漏)
- `mask`: 显式引用 → 替换为掩码表达式; `SELECT *` → 按 information_schema 展开后逐列处理(deny 列剔除)
- `allow` / 未列出: 原样

**行过滤行为**: 对每个引用该表的查询层(主查询 / 子查询 / CTE)分别注入 `WHERE ... AND row_filter`(列引用自动限定表别名)。

## 五、强制执行点

```
LLM(coder) 生成 SQL
   │
   ▼
executor_node ──┬── MCP EXPLAIN 预检(仅复杂 SQL)
   │            └── apply_data_policy(SQL, actor, roles)   ← 强制点 1
   │                  ├─ 拒绝(PolicyDeniedError) → 任务直接 failed, 不回 LLM 自修复
   │                  └─ 改写(掩码/行过滤) → 用改写后 SQL 执行(血缘记录改写后 SQL)
   ▼
LocalSandbox / DockerSandbox(只读事务, 最终兜底)

drill_task_board(下钻) → apply_data_policy(SQL, current_user)   ← 强制点 2
```

- `actor` 由 `execute_task` 从 `task.created_by` 注入 `PipelineState`
- 拒绝原因写入 `error_log`, 前端/审计可见; 权限拒绝**不进入 coder 重试**(权限不可通过重写绕过)

## 六、默认种子(启动幂等)

| 角色 | customers | suppliers |
| --- | --- | --- |
| `user` | phone / id_card / address → `mask` | contact_phone → `mask` |
| `approver` | 同上 → `allow`(豁免) | → `allow` |
| `admin` | 同上 → `allow`(豁免) | → `allow` |

> 豁免原因: 标准账号 roles 含 `user`, 若不给高权限角色配豁免, user 的掩码会粘在审批人/管理员身上。
> 金额/成本等业务指标列不预置, 避免破坏日常销售分析; 需要时管理员在「数据权限」页配置。

## 七、管理界面与 API

- 页面: `web/src/views/admin/DataPolicies.vue`(菜单「数据权限」, 仅 admin)
- API(全部 `require_role("admin")`, 变更写 `audit_logs` 审计):
  - `GET/POST /admin/data-policies`
  - `PUT/DELETE /admin/data-policies/{id}`
- 输入校验: 列模式白名单; `row_filter`/`mask_expression` 必须可解析且禁分号/多语句; 重复(目标+表)返回 409

## 八、已知限制与后续增强

1. **无法确定列归属时保守拒绝**: 多表 JOIN 中未限定的列名命中策略 → 拒绝并提示加别名(同名列如 `unit_price` 存在此风险, coder 生成的 SQL 一般带别名)
2. **SELECT \***: 依赖 `information_schema` 展开; 查不到列清单(表不存在/连接失败)时拒绝
3. **策略无缓存**: 每次执行实时查库, 权限变更立即生效(零泄漏窗口); 高频场景可加 TTL 缓存
4. **coder schema 注入未裁剪**: 目前 deny 列仍会注入 coder 提示(执行层会拒绝); 后续可在 `schema_provider` 按用户权限裁剪/标注, 减少 LLM 生成越权 SQL 的频率
5. **DB 原生 RLS 未启用**: 应用层改写是主强制; 若业务库启用 PG RLS + 受限账号可作第二道兜底(需数据库管理员配合)

## 九、测试

`tests/unit/test_data_policy.py`(18 用例): 策略合并(用户覆盖/多角色宽松/行过滤 AND/停用忽略)、改写引擎(行过滤主/子查询注入、掩码、自定义掩码、deny 拒绝、归属不明拒绝、单表裸列、`SELECT *` 展开与全 deny 拒绝)、executor 强制(拒绝抛 `PolicyDeniedError`、改写后执行)、管理 API(CRUD/非法输入/重复 409/非 admin 403)。
