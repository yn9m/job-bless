# Стандарт разработки и архитектурная философия (Code Style & Guidelines)

Данный документ описывает правила архитектуры, стиль написания кода и структуру проекта `career-agent` (`job-bless`). Документ обязателен для руководства при добавлении новых модулей, адаптеров и сервисов.

---

## 🏛 1. Архитектурная философия

Проект базируется на принципах **Clean Architecture / Hexagonal Architecture (Ports & Adapters)**.

```text
[   Внешний мир / gRPC / CLI / Docker   ]
                 ↓
[ Adapters: internal/client, internal/browser ]
                 ↓  (Реализуют порты)
[ Service Layer: internal/service/deps.go     ]  <-- Зависимости направлены ВНУТРЬ
                 ↓
[ Domain Entities: internal/pkg/ds           ]  <-- Чистый Go без фреймворков
```

### Главные правила:
1. **Направление зависимостей — строго внутрь**:
   - `internal/pkg/ds` — фундаментальные доменные типы и DTO. Не содержит зависимостей от сторонних библиотек, gRPC или протобуфов.
   - `internal/service` — бизнесовая логика (оркестрация). Зависает **только** от интерфейсов (портов), определенных в `internal/service/deps.go`.
   - `internal/client`, `internal/browser`, `internal/model` — адаптеры, реализующие интерфейсы сервиса.
2. **Отсутствие спагетти в `cmd/main.go`**:
   - `main.go` — минимальная точка входа. Запрещено инстанцировать объекты или настраивать логику напрямую. Вся сборка выполняется через **Uber Fx**.

---

## ⚡ 2. Внедрение зависимостей (Uber Fx)

1. **Модульность**:
   - Каждая поддиректория (`config`, `logger`, `browser`, `service`, `client/...`) содержит файл `module.go` с экспортируемой функцией:
     ```go
     func Module(name string) fx.Option {
         return fx.Module(
             name,
             fx.Provide(New...),
         )
     }
     ```
2. **Связывание Интерфейсов и Реализаций**:
   - Регистрация интерфейсов к их конкретным реализациям выполняется в корневом модуле `internal/app/module.go` с помощью `fx.Annotate` и `fx.As`:
     ```go
     fx.Provide(
         fx.Annotate(
             func(b *browser.Manager) service.BrowserProvider { return b },
         ),
     )
     ```
3. **Управление жизненным циклом (Lifecycle Hooks)**:
   - Любое открытие/закрытие ресурсов (gRPC-соединения, фоновые воркеры, HTTP-серверы, браузерные процессы) **обязано** регистрироваться в `fx.Lifecycle`:
     ```go
     lc.Append(fx.Hook{
         OnStart: func(ctx context.Context) error { ... },
         OnStop:  func(ctx context.Context) error { ... },
     })
     ```

---

## 🧩 3. Подключаемые модули (Pluggable Architecture)

Каждая новая фича или интеграция (например, `hh-autoscroller`, `linkedin-applier`, `superjob-parser`) оформляется как самостоятельный модуль:

1. **Protobuf Контракт**:
   `api/proto/<platform>/<feature>/<version>/<feature>.proto`
2. **Сгенерированный код**:
   `gen/<platform>/<feature>/<version>/`
3. **Клиентский Адаптер**:
   `internal/client/<feature>/` (файлы `client.go` и `module.go`)
4. **Порт в Сервисе**:
   Добавление соответствующего интерфейса в `internal/service/deps.go`.

---

## 📝 4. Конфигурация и Логирование

### Логирование (`go.uber.org/zap`)
- Использование `fmt.Println` или глобального `log` **запрещено**.
- В конструкторы компонентов передается `*zap.Logger`.
- Логирование выполняется структурированно:
  ```go
  logger.Info("task status updated", zap.String("task_id", id), zap.Error(err))
  ```

### Конфигурация
- Сосредоточена в `configs/config.yaml`.
- Модель конфигурации определяется в `internal/config/config.go` с тегами `yaml:"..."`.
- Переменная окружения `CONFIG_PATH` позволяет переопределить путь к YAML-файлу.

---

## 🛠 5. Кодстайл и Правила Горутин

1. **Обработка ошибок**:
   - Все ошибки оборачиваются с контекстом через `fmt.Errorf("doing operation: %w", err)`.
2. **Управление фоновыми задачами**:
   - Фоновые горутины обязаны принимать `context.Context` или завершаться по `ctx.Done()`.
3. **Конструкторы**:
   - Именоваться по стандарту `New` (если один в пакете) или `New<TypeName>` (если несколько).
