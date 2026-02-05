# Tasks

> Задачи с приоритетами, зависимостями и трассировкой к требованиям

## Легенда

**Приоритет:**
- 🔴 P0 — Critical, блокирует релиз
- 🟠 P1 — High, нужно для полноценного использования
- 🟡 P2 — Medium, улучшение опыта
- 🟢 P3 — Low, nice to have

**Статус:**
- ⬜ TODO
- 🔄 IN PROGRESS
- ✅ DONE
- ⏸️ BLOCKED

**Оценка:**
- Указывай в днях (d) или часах (h)
- Лучше диапазон: 3-5d

---

## Definition of Done (для КАЖДОЙ задачи)

> ⚠️ Задача НЕ считается завершённой без выполнения этих пунктов:

- [ ] **Unit tests** — покрытие ≥80% нового кода
- [ ] **Tests pass** — все тесты проходят локально
- [ ] **Integration test** — если изменены публичные интерфейсы
- [ ] **CI green** — pipeline проходит
- [ ] **Docs updated** — документация актуальна
- [ ] **Code review** — PR approved

---

## Testing Tasks (обязательные)

### TASK-100: Test Infrastructure Setup
🔴 P0 | ⬜ TODO | Est: 2d

**Description:**  
Настроить тестовую инфраструктуру.

**Checklist:**
- [ ] Test framework setup (pytest/jest/etc.)
- [ ] Coverage reporting
- [ ] CI workflow
- [ ] Test fixtures structure
- [ ] Pre-commit hooks

**Traces to:** [NFR-000]  
**Depends on:** —  
**Blocks:** All other tasks

---

## Milestone 1: MVP

### TASK-001: {{TASK_NAME}}
🔴 P0 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}
- [ ] {{SUBTASK_3}}

**Tests (Definition of Done):**
- [ ] Unit tests: {{test_scope_1}}
- [ ] Unit tests: {{test_scope_2}}
- [ ] Integration test (если нужен)
- [ ] Coverage ≥80%

**Traces to:** [REQ-XXX], [REQ-YYY]  
**Depends on:** [TASK-100]  
**Blocks:** [TASK-XXX]

---

### TASK-002: {{TASK_NAME}}
🔴 P0 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-001]  
**Blocks:** [TASK-XXX]

---

### TASK-003: {{TASK_NAME}}
🟠 P1 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-001], [TASK-002]  
**Blocks:** —

---

## Milestone 2: Beta

### TASK-010: {{TASK_NAME}}
🟠 P1 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

### TASK-011: {{TASK_NAME}}
🟡 P2 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

## Milestone 3: GA

### TASK-020: {{TASK_NAME}}
🟡 P2 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

### TASK-021: {{TASK_NAME}}
🟢 P3 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

## Dependency Graph

```
TASK-001 ({{name}})
    │
    ├──► TASK-002 ({{name}})
    │        │
    │        └──► TASK-003 ({{name}})
    │
    └──► TASK-010 ({{name}})
             │
             └──► TASK-011 ({{name}})
                      │
                      └──► TASK-020 ({{name}})
```

---

## Summary by Milestone

### MVP
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | {{X}} | {{Y}}d |
| 🟠 P1 | {{X}} | {{Y}}d |
| 🟡 P2 | {{X}} | {{Y}}d |
| **Total** | **{{X}}** | **~{{Y}}d** |

### Beta
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | {{X}} | {{Y}}d |
| 🟠 P1 | {{X}} | {{Y}}d |
| 🟡 P2 | {{X}} | {{Y}}d |
| **Total** | **{{X}}** | **~{{Y}}d** |

### GA
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | {{X}} | {{Y}}d |
| 🟠 P1 | {{X}} | {{Y}}d |
| 🟡 P2 | {{X}} | {{Y}}d |
| 🟢 P3 | {{X}} | {{Y}}d |
| **Total** | **{{X}}** | **~{{Y}}d** |

---

## Risk Register

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| {{RISK_1}} | High/Med/Low | High/Med/Low | {{MITIGATION}} |
| {{RISK_2}} | High/Med/Low | High/Med/Low | {{MITIGATION}} |

---

## Notes

- {{NOTE_1}}
- {{NOTE_2}}
