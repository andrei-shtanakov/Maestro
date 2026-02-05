# Requirements Specification

> {{PROJECT_NAME}} — {{PROJECT_TAGLINE}}

## 1. Контекст и цели

### 1.1 Проблема

{{PROBLEM_DESCRIPTION}}

### 1.2 Цели проекта

| ID | Цель | Метрика успеха |
|----|------|----------------|
| G-1 | {{GOAL_1}} | {{METRIC_1}} |
| G-2 | {{GOAL_2}} | {{METRIC_2}} |
| G-3 | {{GOAL_3}} | {{METRIC_3}} |

### 1.3 Стейкхолдеры

| Роль | Интересы | Влияние |
|------|----------|---------|
| {{ROLE_1}} | {{INTERESTS_1}} | Высокое/Среднее/Низкое |
| {{ROLE_2}} | {{INTERESTS_2}} | Высокое/Среднее/Низкое |

### 1.4 Out of Scope

> ⚠️ Явно укажи, что НЕ входит в проект

- ❌ {{OUT_OF_SCOPE_1}}
- ❌ {{OUT_OF_SCOPE_2}}
- ❌ {{OUT_OF_SCOPE_3}}

---

## 2. Функциональные требования

### 2.1 {{FEATURE_GROUP_1}}

#### REQ-001: {{REQUIREMENT_NAME}}
**As a** {{ROLE}}  
**I want** {{ACTION}}  
**So that** {{VALUE}}

**Acceptance Criteria:**
```gherkin
GIVEN {{PRECONDITION}}
WHEN {{ACTION}}
THEN {{RESULT}}
AND {{ADDITIONAL_RESULT}}
```

**Priority:** P0 | P1 | P2 | P3  
**Traces to:** [TASK-XXX], [DESIGN-XXX]

---

#### REQ-002: {{REQUIREMENT_NAME}}
**As a** {{ROLE}}  
**I want** {{ACTION}}  
**So that** {{VALUE}}

**Acceptance Criteria:**
```gherkin
GIVEN {{PRECONDITION}}
WHEN {{ACTION}}
THEN {{RESULT}}
```

**Priority:** P0 | P1 | P2 | P3  
**Traces to:** [TASK-XXX], [DESIGN-XXX]

---

### 2.2 {{FEATURE_GROUP_2}}

#### REQ-010: {{REQUIREMENT_NAME}}
...

---

## 3. Нефункциональные требования

### NFR-000: Testing Requirements
| Аспект | Требование |
|--------|------------|
| Unit test coverage | ≥ {{COVERAGE}}% для core modules |
| Integration tests | Каждый ключевой компонент |
| Test framework | {{TEST_FRAMEWORK}} |
| CI requirement | Все тесты проходят перед merge |

**Definition of Done для любой задачи:**
- [ ] Unit tests написаны и проходят
- [ ] Coverage не упал
- [ ] Integration test если затронуты интерфейсы
- [ ] Документация обновлена

**Traces to:** [TASK-100]

---

### NFR-001: Performance
| Метрика | Требование |
|---------|------------|
| {{METRIC}} | {{VALUE}} |

**Traces to:** [TASK-XXX]

---

### NFR-002: Security
| Аспект | Требование |
|--------|------------|
| {{ASPECT}} | {{REQUIREMENT}} |

**Traces to:** [TASK-XXX]

---

### NFR-003: Usability
| Метрика | Требование |
|---------|------------|
| {{METRIC}} | {{VALUE}} |

**Traces to:** [TASK-XXX]

---

## 4. Ограничения и техстек

### 4.1 Технологические ограничения

| Аспект | Решение | Обоснование |
|--------|---------|-------------|
| Язык | {{LANGUAGE}} | {{RATIONALE}} |
| База данных | {{DB}} | {{RATIONALE}} |
| Инфраструктура | {{INFRA}} | {{RATIONALE}} |

### 4.2 Интеграционные ограничения

- {{INTEGRATION_CONSTRAINT_1}}
- {{INTEGRATION_CONSTRAINT_2}}

### 4.3 Бизнес-ограничения

- Бюджет: {{BUDGET}}
- Сроки: {{TIMELINE}}
- Команда: {{TEAM_SIZE}}

---

## 5. Критерии приёмки

### Milestone 1: MVP
- [ ] REQ-001 — {{DESCRIPTION}}
- [ ] REQ-002 — {{DESCRIPTION}}
- [ ] NFR-001 — {{DESCRIPTION}}

### Milestone 2: Beta
- [ ] REQ-010 — {{DESCRIPTION}}
- [ ] REQ-011 — {{DESCRIPTION}}

### Milestone 3: GA
- [ ] All P0 and P1 requirements implemented
- [ ] {{ADDITIONAL_CRITERIA}}
