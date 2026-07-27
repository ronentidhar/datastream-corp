---
name: data-integrity-triage
description: Use when investigating data-integrity anomalies in the DataStream Corp database — overdue projects, orphaned project assignments, or payroll figures that do not reconcile against department budgets. Provides the exact queries and the severity rules.
---

# Data-integrity triage

Follow this procedure exactly. Do not invent alternative queries.

## 1. Overdue projects

```sql
SELECT id, name, status, budget, end_date,
       CAST(julianday('now') - julianday(end_date) AS INT) AS days_overdue
FROM projects
WHERE status IN ('active', 'planning')
  AND end_date < date('now')
ORDER BY end_date ASC;
```

A project still `active` or `planning` past its `end_date` was never closed
out. `days_overdue` drives severity — over 365 days is `critical`.

## 2. Orphaned assignments

```sql
SELECT p.id, p.name, p.status, COUNT(*) AS open_assignments
FROM project_assignments a
JOIN projects p ON a.project_id = p.id
WHERE p.status = 'completed' AND a.end_date IS NULL
GROUP BY p.id
ORDER BY open_assignments DESC;
```

People still booked to a project that is finished. Real cost: their capacity
looks consumed when it is not.

## 3. Payroll vs department budget

```sql
SELECT d.name, d.budget, ROUND(SUM(e.salary)) AS payroll,
       ROUND(1.0 * SUM(e.salary) / d.budget, 1) AS ratio
FROM employees e
JOIN departments d ON e.department_id = d.id
WHERE e.status = 'active'
GROUP BY d.id
ORDER BY ratio DESC;
```

`budget` and summed `salary` should be reconcilable. A `ratio` above 2.0 means
they are not — either `budget` excludes payroll, or the salary figures are on
the wrong scale. Report the ratio; do not guess which side is wrong.

## 4. Severity

| Condition | Severity |
|---|---|
| Any department with `ratio` > 2.0 | `critical` |
| Any project more than 365 days overdue | `critical` |
| Projects overdue by less than a year | `warning` |
| Orphaned assignments only | `warning` |
| None of the above | `info` |

## 5. Remediation

Propose, never execute. A recommended action names the system to change and
the identifiers involved, e.g. "close projects 2 and 4 via the PMO tool after
sign-off, and end the 10 open assignments on project 5".

Writing the fix to the database yourself is prohibited — see CLAUDE.md. The
`query_db` tool will happily accept an `UPDATE`; the hook will not.
