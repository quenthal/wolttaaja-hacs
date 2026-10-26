# Wolttaaja

An independent, unofficial Wolt integration for Home Assistant. Three permanent sensors,
one shared asynchronous coordinator, and grouped arrival events. No per-order entities,
location tracking, product lists or order history. Supports one Wolt account per HA instance.

## Install with HACS

Requires Home Assistant **2026.7.0+**. Tested with HA 2026.9.4 and Python 3.14.

1. In HACS, open **Custom repositories**, add
   `https://github.com/quenthal/wolttaaja-hacs`, category **Integration**.
2. Download Wolttaaja and restart Home Assistant. Enable beta versions if selecting a prerelease.
3. Open **Settings → Devices & services → Add integration → Wolttaaja**.
4. Supply your refresh token; access token and session ID are optional.

Without a GitHub release, HACS installs the default branch. This repository uses the standard
`custom_components/wolttaaja` layout, not release ZIP assets. Manual fallback: copy that directory
to `/config/custom_components/wolttaaja`, then restart. This is a separate integration from
Wait for Wolt; disable the old integration to avoid duplicate Wolt polling.

## Credentials

Following [selfish's authentication documentation](https://github.com/selfish/ha-wait-for-wolt#authentication):
sign in to **https://wolt.com** in your browser. Open developer tools (F12), then Application/Storage
and the cookies for `https://wolt.com`:

- `__wrtoken`: refresh credential (`refreshToken` or `refresh_token` inside decoded JSON).
- `__wtoken`: optional access credential (`accessToken` or `access_token`).
- `__woltUid`: optional session ID; it can be absent without analytics consent.

Cookie values may be URL-encoded. Copy the token string inside the decoded value, not the JSON
wrapper. If unavailable, inspect an authenticated Wolt request locally. Never send tokens to
another website, paste them in an issue, or commit them. Wolttaaja does not collect your password,
phone number, email, or login codes. There is no public Wolt consumer OAuth integration.

Credentials are validated on setup. A missing access token triggers refresh; an HTTP 401 triggers
one refresh and retry. Rotated tokens are saved in the HA config entry. Rejected credentials
trigger Home Assistant reauthentication. HTTP 403 is treated as a temporary connection failure,
not as bad credentials, because bot protection in front of Wolt also answers 403. Tokens and API bodies are never logged by this integration.

## Entities

The default entity IDs below assume they are not already occupied; HA can append a suffix.
All three have permanent unique IDs tied to the config entry. Names and status labels are
translated (English and Finnish); entity IDs and state values stay the same in every language.

| Entity | State | Attributes |
| --- | --- | --- |
| `sensor.wolttaaja_next_delivery` | Earliest active ETA, timestamp | `order_id`, `restaurant`, `status`, `eta` |
| `sensor.wolttaaja_order_status` | Status of the earliest-ETA order | `order_id`, `restaurant`, `status`, `eta` |
| `sensor.wolttaaja_active_orders` | Integer count | `orders`: list of the four fields above |

`next_delivery` and `order_status` always select the same order from the same coordinator
snapshot: the earliest known ETA. They switch together when ETA ordering changes, including
all four order attributes. Equal ETAs use stable ID order. Missing ETAs sort last; if all are
missing, stable ID selects the order and its restaurant/status while the timestamp is unknown.
With no orders, both sensors are unknown and the count is zero. All sensors become unavailable
when an API failure prevents a reliable complete snapshot, including a failed details request.

Statuses normalize to `pending`, `preparing`, `ready_for_pickup`, `picked_up`, `on_the_way`,
`arriving`, `delivered`, `cancelled`, `failed`, or `unknown`. Unknown text is never exposed raw.
`order_status` is an enum sensor; an unrecognized status shows as HA's `unknown` state while the
`status` attribute keeps `unknown`. Wolt's order-list telemetry determines which orders remain active.
An order whose lifecycle cannot be read is ignored (logged once per value) unless it is already
being tracked; a tracked order that becomes unreadable makes the sensors unavailable instead of
counting as completed. A completed/cancelled order,
or one absent from a successful full order-list response, leaves the active snapshot.

Orders retain Wolt purchase IDs: same restaurant, courier or ETA never merges identities.
The `orders` list uses first-observed order, independent of ETA. For example, `[1, 2, 3]`
stays in that order when order 3 becomes the next delivery; once 3 completes it becomes `[1, 2]`.
This order survives restarts. Newly observed orders append to the list; orders first seen in
the same poll use API response order. The verified API has no reliable creation-time field,
so this is observation order rather than a guessed purchase chronology.

Completed/cancelled orders (or orders absent from a successful full list) leave immediately
on that update. There is no completion cooldown or missing-order grace period. API failures
never count as completion. The count returns to zero and idle polling resumes after the last
order leaves. Arrival notification deduplication is independent of which order is selected.

### Upgrading from the four-entity version

The retired `binary_sensor.wolttaaja_delivery_active` is removed from this integration's entity
registry once, when the config entry is migrated, even if renamed. The three sensor unique IDs remain unchanged.
Replace binary-sensor conditions with a numeric-state condition on `sensor.wolttaaja_active_orders`
(`above: 0`). For a template, check `has_value('sensor.wolttaaja_active_orders')` before converting
the state to a number: unavailable is not the same as zero. Existing notification IDs remain
stored; the obsolete sticky primary is discarded. List ordering begins with the first successful
poll after upgrade. Update any cards or automations using the retired entity.

## Options

Change these under Wolttaaja's **Configure** menu; no reinstallation is needed.

| Setting | Default | Range |
| --- | --- | --- |
| Idle polling interval | 300 seconds | 60–1800 seconds |
| Active polling interval | 30 seconds | 15–300 seconds |
| ETA notification lead | 5 minutes | 1–15 minutes |
| Notification grouping window | 3 minutes | 0–10 minutes |
| ETA delay event threshold | 10 minutes | 0–60 minutes (0 = off) |

Each successful cycle fetches the order list once and details once per active order, concurrently.
The client remembers which of Wolt's two details URL forms answers and starts from it.
Polling switches between idle and active automatically. HTTP 429 honors numeric and HTTP-date
`Retry-After` values; invalid/missing headers use 60 seconds. The API client also blocks manual
refreshes during cooldown, and the coordinator persists the cooldown across restart.

## Arrival events

Listen for `wolttaaja_delivery_approaching`:

```yaml
order_count: 2
notification_window_minutes: 5
grouping_window_minutes: 3
orders:
  - order_id: "12345"
    restaurant: "Kotipizza"
    eta: "2026-10-19T18:30:00+03:00"
    status: "on_the_way"
  - order_id: "67890"
    restaurant: "Hesburger"
    eta: "2026-10-19T18:32:00+03:00"
    status: "on_the_way"
```

When an unnotified ETA enters the lead window, an event fires on that poll. Other unnotified
orders within the grouping window of the anchor ETA join it immediately, even if slightly
outside the lead window. Groups do not chain transitively. A zero window groups only identical
ETAs. Several separate groups can fire in one cycle. A newly discovered order still gets its
own event after a previous group has fired. No ETA means no notification. An ETA that already
passed without a notification (for example a courier ahead of schedule between two polls, or a
restart) fires late on the next successful poll rather than never. A changed ETA does not re-arm
an already notified order; see Delay events below.

### Delay events

After an order has been announced, `wolttaaja_delivery_delayed` fires when its ETA moves later
by at least the delay threshold. The arrival event is not repeated; your automation decides
whether to react. Later delays are measured from the ETA in the most recent event, so normal
ETA jitter and a slow drift below the threshold stay quiet. Earlier ETAs never fire. Orders that
have not been announced yet do not produce delay events.

```yaml
order_count: 1
delay_threshold_minutes: 10
orders:
  - order_id: "12345"
    restaurant: "Kotipizza"
    eta: "2026-10-19T18:42:00+03:00"
    status: "on_the_way"
    previous_eta: "2026-10-19T18:30:00+03:00"
    delay_minutes: 12
```

Notification IDs, last announced ETAs and active list ordering live in Home Assistant `Store`, separate from Recorder. Inactive
notification IDs expire after 30 days; still-active IDs are retained. Removal of the integration
removes its Store. No raw order history is persisted.

### Phone notification

Replace the notify action with your phone's service:

```yaml
alias: Wolttaaja phone notification
triggers:
  - trigger: event
    event_type: wolttaaja_delivery_approaching
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: "Wolt delivery approaching"
      message: >-
        {{ trigger.event.data.order_count }} delivery/deliveries approaching:
        {{ trigger.event.data.orders | map(attribute='restaurant') | join(', ') }}.
mode: queued
```

### Text to speech

Replace the TTS entity and media player:

```yaml
alias: Wolttaaja spoken notification
triggers:
  - trigger: event
    event_type: wolttaaja_delivery_approaching
actions:
  - action: tts.speak
    target:
      entity_id: tts.your_tts_provider
    data:
      media_player_entity_id: media_player.living_room
      message: >-
        Your Wolt delivery is approaching from
        {{ trigger.event.data.orders | map(attribute='restaurant') | join(' and ') }}.
mode: queued
```

## Privacy and Recorder

Only order ID, restaurant, normalized status and ETA are published. The integration uses HA's
`_unrecorded_attributes` for all order attributes, including `orders`. Scalar state changes can
still enter Recorder. To exclude all three sensors entirely:

```yaml
recorder:
  exclude:
    entities:
      - sensor.wolttaaja_next_delivery
      - sensor.wolttaaja_order_status
      - sensor.wolttaaja_active_orders
```

Merge this with your existing Recorder configuration. Automations, traces, notifications, backups
and other event consumers may store event data. Credentials in HA config entries and opaque order
IDs in `.storage` should be protected with your HA configuration and backups.

## Limitations and validation

- Wolt's private API can change or omit ETA/status data; this is not endorsed by Wolt.
- Detection is polling-based. A new order can take up to the idle interval to appear. Missing
  orders in Wolt's list cannot be reconstructed; no pagination contract is assumed.
- Persistent deduplication is at-most-once during normal operation. Saving IDs before firing
  prevents restart duplicates, but a crash between saving and dispatch can lose an event.
  HA events are not a durable message queue; disabled automations do not receive old events.
- Storage failure prevents event dispatch. Restoring an old HA backup can restore old dedup state.
- No live Wolt account or physical phone/TTS delivery was available for validation. Test with
  real orders before relying on this for time-critical automations.

Run offline tests on Linux with `uv sync --python 3.14`, then `uv run pytest -q`,
`uv run ruff check .` and `uv run ruff format --check .`. CI also runs Hassfest and HACS validation.
Tests cover lifecycle, multi-order selection, grouping, restart persistence, API authentication,
rate limits, config flow, three real HA entities, translations, options and unload.

## Credits and license

Based in part on **[Wait for Wolt by selfish](https://github.com/selfish/ha-wait-for-wolt)**,
audited at commit `1566152f53e43bd74d5a4056af212c53b62e064f` (2026-10-19).
The asynchronous API client, token refresh flow, status/ETA parsers and API tests are adapted
from that MIT-licensed project. Its `Copyright (c) 2026 selfish` notice and full license are
preserved in [LICENSE](LICENSE) and inside the installed integration.

The supplied starter prototype informed the original four-entity design. Wolttaaja replaces its in-memory
selector and patch workflow with an independent integration, persistent notification/list state, synchronized next-delivery sensors, options,
notifications and tests. It does not depend on Wait for Wolt being installed.

The Wolt name and logo are trademarks of Wolt Enterprises Oy. They are used only to identify
the service this integration connects to; Wolttaaja is not affiliated with or endorsed by Wolt.
