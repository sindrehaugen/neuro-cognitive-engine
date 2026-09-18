# Notifications Engine User Guide

## 1. Purpose

The Notifications engine (C13, Wave A-3) gives every principal a persistent inbox
(`notifications`) and a way to set reminders against any knowledge-graph node
(`reminders`), each exposed as a C12 resource surface (§2). It also carries real
hand-written automation (`nce/vertical_modules/notifications/subscribers.py`):
seven outbox-event subscribers that translate cross-engine events into
notifications automatically, with idempotent replay (§3).

## 2. Resource Surface (C12, Wave A-3)

Two declarative `ResourceSpec`s live in
`nce/vertical_modules/notifications/resources.py`, both under the `notifications`
engine slug.

### 2.1 NOTIFICATION (`notifications` table)

| Field | Role |
|---|---|
| `principal_id` | Who the notification is for |
| `title`, `body` | Content |
| `severity`, `category` | Classification |
| `source_selector`, `source_id` | What produced it (e.g. an engine + entity ID) |
| `read_at`, `seen_at` | Read/seen tracking |
| `is_archived` | Soft-delete flag |

Filterable: `principal_id`, `severity`, `category`, `source_selector`,
`source_id`. Searchable (`?q=`): `title`, `body`.

### 2.2 REMINDER (`reminders` table)

| Field | Role |
|---|---|
| `principal_id` | Who set the reminder |
| `node_type`, `node_id` | The knowledge-graph node it's attached to |
| `title`, `note` | Content |
| `remind_at`, `fired_at` | Scheduling / firing state |
| `status` | Lifecycle state |
| `is_archived` | Soft-delete flag |

Filterable: `principal_id`, `node_type`, `node_id`, `status`. Searchable
(`?q=`): `title`, `note`.

Neither spec declares a `tier_allowlists` override, so principal-tier redaction
falls back to the C12 default (full record for the owning principal, subject to
the same tenant-namespace isolation as every other resource).

### 2.3 MCP Tools (8)

| Tool Name | Cacheable | Mutation | Description |
|---|:---:|:---:|---|
| `notifications_list_notifications` | ✔ | ✘ | List/query a principal's notifications. |
| `notifications_get_notifications` | ✔ | ✘ | Fetch a single notification by ID. |
| `notifications_upsert_notifications` | ✘ | ✔ | Create or update a notification. |
| `notifications_archive_notifications` | ✘ | ✔ | Soft-archive a notification. |
| `notifications_list_reminders` | ✔ | ✘ | List/query a principal's reminders. |
| `notifications_get_reminders` | ✔ | ✘ | Fetch a single reminder by ID. |
| `notifications_upsert_reminders` | ✘ | ✔ | Create or update a reminder. |
| `notifications_archive_reminders` | ✘ | ✔ | Soft-archive a reminder. |

None are `admin_only`.

### 2.4 REST Routes (32)

Two independent 16-route C12 mounts, one per entity, both under
`nce.resource_surface.rest`:

| Base path | Entity |
|---|---|
| `/api/notifications/notifications` | NOTIFICATION |
| `/api/notifications/reminders` | REMINDER |

Each base path exposes the standard C12 verb set: `GET`/`POST` list+create,
`POST .../bulk`, `GET`/`PATCH .../{id}`, `POST .../{id}/archive`,
`POST .../{id}/restore`, `GET .../{id}/events`, `GET`/`POST .../{id}/comments`,
`GET`/`POST .../{id}/tags`, `DELETE .../{id}/tags/{tag}`, `GET`/`POST
.../{id}/documents` (generic C12 document attachment, Wave A-4), `DELETE
.../{id}/documents/{doc_id}` — 16 routes each, 32 total.

### 2.5 Storage and Tenancy

Both `notifications` and `reminders` are tenant-scoped tables
(`tenant_scope == "tenant"`, from `EXPECTED_TENANT_RLS_TABLES` in
`nce/event_log.py`) — every read and write is isolated to the caller's
`namespace_id` by Postgres RLS.

## 3. Automatic Notification Generation (Event Subscribers)

`nce/vertical_modules/notifications/subscribers.py::register_notifications_subscribers()`
registers seven handlers against the transactional outbox
(`nce.events.bus.subscribe`), each translating a cross-engine event into a
persistent notification for the relevant principal(s), with a deterministic
`idempotency_key` so replay never double-delivers:

| Event | Handler |
|---|---|
| `TICKET.sla_breached` | `handle_ticket_sla_breached` |
| `CERTIFICATION.EXPIRED` | `handle_certification_expired` |
| `PO_LINE.status_changed` | `handle_po_line_status_changed` |
| `AGREEMENT.renewal_due` | `handle_agreement_renewal_due` |
| `DEAL.stalled` | `handle_deal_stalled` |
| `ASSET.health_changed` | `handle_asset_health_changed` |
| `REMINDER.fired` | `handle_reminder_fired` |

Delivery here means an in-app inbox row via `service.py::create_notification` —
not email/SMS/webhook fan-out, and not a scheduler: a reminder's `remind_at`
firing (the `REMINDER.fired` event) is produced elsewhere and only consumed
here.

## 4. What this engine deliberately does not do

It does not send email, SMS, or webhook notifications, and it does not run the
scheduler that decides when a reminder's `remind_at` has been reached — both are
out of scope; this engine persists and queries notifications/reminders, and
reacts to events other components already decided to emit.
