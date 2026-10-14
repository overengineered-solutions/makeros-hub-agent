# Laser: the Home Assistant side (OMTech)

Hub agent v0.68.0 reads the OMTech's plug through Home Assistant and asks HA to switch it on. This file holds the HA
YAML for both. It is installed by the orchestrator **only with the operator's go-ahead, after an HA snapshot**. Redact
`webhook_id` in every export before committing it.

- **Telemetry (5a)**: HA pushes the plug's switch state, current and power to the hub every 30 s and on every threshold
  crossing. It switches nothing. The hub holds no HA token.
- **Turn on (5b)**: the hub POSTs `{"requestId", "laser"}` to an HA webhook when the member holding the session taps
  Turn on from the shop network. HA checks the exhaust, the fans, the person sensor and the plug, then runs
  `switch.turn_on`. That is the only device action. Nothing here, in the hub or in pstation can switch the laser off.

## Secrets (paths only; the operator mints every value through the secret lane)

| File (0600, owned by makeros-hub) | Holds | HA's copy |
|---|---|---|
| `/var/lib/makeros-hub/ha-laser-push-token` | the bearer HA presents to the hub | `secrets.yaml`: `ps_hub_laser_push_auth: "Bearer <same value>"` |
| `/var/lib/makeros-hub/ha-laser-webhook-url` | `http://<HA>:8123/api/webhook/<id>`; the id is the secret | the `webhook_id` in 5b |

A missing, empty or group/world-readable file means "not configured" on the hub (Home Assistant: not connected, or
"Turning on from the site isn't connected at the shop yet"). The hub never logs either value or the URL.

## The hub's `[laser]` table (`/etc/makeros-hub/config.toml`)

```toml
[laser]
ha_push_listen = "0.0.0.0:8788"
ha_push_allowed_ip = "192.168.1.213"   # HA (HAOS Pi); every other source gets 403
gate_url = "http://127.0.0.1:7650"     # omit until the gate exists; http to loopback or a private IPv4 only
# gate_laser = "omtech-pro-80w"        # only needed once config-down lists more than one laser
cutting_amps = 2.0
on_amps = 0.5
run_gap_s = 60
```

An unknown key or a bad value is logged and leaves every laser "not configured"; the printers are unaffected. Keep
8788/tcp reachable from HA only (the listener also refuses any other source address).

## Before installing: confirm

- `binary_sensor.ps_laser_corner_person`: the corner camera appears twice in HA (plan Q10). Use the live one.
- `automation.laser_cutter_6_exhaust_fans_on` and `automation.laser_cutter_6_exhaust_fans_off`: ids derived from the
  aliases; confirm with `./ha states`.
- The fan plugs `switch.mini_smart_wi_fi_plug`, `_2`, `_3` and the laser plug
  `switch.athom_smart_plug_v2_9d97be_switch` with its `_current` / `_power` sensors.

## (5a) Telemetry, read-only

Part one goes in `configuration.yaml` (an operator edit with the File editor; then reload or restart HA). The secret
goes in `secrets.yaml`.

```yaml
rest_command:
  ps_hub_laser_state:
    url: "http://<shop-box LAN address>:8788/ha/laser"
    method: POST
    timeout: 5
    headers:
      Authorization: !secret ps_hub_laser_push_auth   # "Bearer <value of /var/lib/makeros-hub/ha-laser-push-token>"
    content_type: "application/json"
    payload: >-
      {"laser": "omtech-pro-80w",
       "switch": "{{ states('switch.athom_smart_plug_v2_9d97be_switch') }}",
       "currentA": "{{ states('sensor.athom_smart_plug_v2_9d97be_current') }}",
       "powerW": "{{ states('sensor.athom_smart_plug_v2_9d97be_power') }}"}
```

```yaml
id: ps_laser_push_omtech
alias: "Laser - push plug readings to the shop hub (OMTech)"
description: "Telemetry for pstation's laser status and run tracking. Switches nothing."
mode: queued
max: 3
max_exceeded: silent
triggers:
  - {trigger: numeric_state, entity_id: sensor.athom_smart_plug_v2_9d97be_current, above: 2}
  - {trigger: numeric_state, entity_id: sensor.athom_smart_plug_v2_9d97be_current, below: 2}
  - {trigger: numeric_state, entity_id: sensor.athom_smart_plug_v2_9d97be_current, above: 0.5}
  - {trigger: numeric_state, entity_id: sensor.athom_smart_plug_v2_9d97be_current, below: 0.5}
  - {trigger: state, entity_id: switch.athom_smart_plug_v2_9d97be_switch}
  - {trigger: time_pattern, seconds: "/30"}
actions:
  - action: rest_command.ps_hub_laser_state
    continue_on_error: true
```

## (5b) Turn on

### Last real sighting (configuration.yaml)

Changed from the contract text, which counted `state == 'off' and now() - last_changed < 120 s` as "seen in the last
2 minutes". `last_changed` also moves on `unavailable`/`unknown` -> `off`, and resets on every HA restart, integration
reload or camera reconnect, so a sensor that never saw anyone read as a recent sighting for 2 minutes. This
trigger-based template sensor records only real sightings: it stamps the time a person is detected and the time the
detection ends (on -> anything), and HA restores it across restarts.

```yaml
template:
  - triggers:
      - trigger: state
        entity_id: binary_sensor.ps_laser_corner_person   # CONFIRM (camera appears twice, plan Q10)
        to: "on"
      - trigger: state
        entity_id: binary_sensor.ps_laser_corner_person
        from: "on"
    sensor:
      - name: "PS laser person last seen"
        unique_id: ps_laser_person_last_seen
        device_class: timestamp
        state: "{{ now().isoformat() }}"
```

The entity is `sensor.ps_laser_person_last_seen`; confirm the id after the reload. Until someone has been seen once it
is `unknown`, and Turn on is refused ("nobody seen at the laser in the last 2 minutes"). The recent-sighting branch also
needs the camera to read `off` right now: while it is `unavailable` or `unknown` nobody can be seen, so Turn on is
refused even if the last sighting is recent.

### The automation

```yaml
id: ps_laser_turn_on_omtech
alias: "Laser - turn on from the site (OMTech)"
description: >-
  The shop hub calls this when the member holding the OMTech session taps Turn on from the shop network. The ONLY device action is
  switch.turn_on on the OMTech plug; nothing here, in the hub or in pstation can switch it off. A refused request alerts instead.
  webhook_id is a secret: redact it in exports/ before committing.
mode: single
max_exceeded: silent
triggers:
  - trigger: webhook
    webhook_id: "<minted by the operator; never committed>"
    allowed_methods: [POST]
    local_only: true
variables:
  laser_plug: switch.athom_smart_plug_v2_9d97be_switch
  person: binary_sensor.ps_laser_corner_person              # CONFIRM (camera appears twice, plan Q10)
  person_seen: sensor.ps_laser_person_last_seen             # the template sensor above
  fans: [switch.mini_smart_wi_fi_plug, switch.mini_smart_wi_fi_plug_2, switch.mini_smart_wi_fi_plug_3]
  exhaust_on: automation.laser_cutter_6_exhaust_fans_on    # CONFIRM entity id
  exhaust_off: automation.laser_cutter_6_exhaust_fans_off  # CONFIRM entity id
actions:
  - variables:
      right_laser: "{{ trigger.json is defined and trigger.json.laser == 'omtech-pro-80w' }}"
      armed: "{{ is_state(exhaust_on, 'on') and is_state(exhaust_off, 'on') }}"
      fans_ok: "{{ (expand(fans) | count) == 3 and (expand(fans) | selectattr('state', 'in', ['unavailable', 'unknown']) | list | count) == 0 }}"
      person_ok: >-
        {% set p = expand(person) | list %}
        {% set seen = as_datetime(states(person_seen), none) %}
        {% set age = (now() - seen).total_seconds() if seen is not none else -1 %}
        {{ p | count == 1 and (p[0].state == 'on' or (p[0].state == 'off' and 0 <= age < 120)) }}
      plug_ok: "{{ states(laser_plug) in ['on', 'off'] }}"
  - if:
      - condition: template
        value_template: "{{ right_laser and armed and fans_ok and person_ok and plug_ok }}"
    then:
      - action: switch.turn_on
        target: {entity_id: "{{ laser_plug }}"}
    else:
      - variables:
          msg: >-
            Turn on from the site was refused:
            {{ 'wrong laser; ' if not right_laser }}{{ 'exhaust automations not armed; ' if not armed }}{{ 'a fan outlet is offline; ' if not fans_ok }}{{ 'nobody seen at the laser in the last 2 minutes; ' if not person_ok }}{{ 'the laser plug is offline; ' if not plug_ok }}
      - action: persistent_notification.create
        data: {title: "Laser turn-on refused", message: "{{ msg }}", notification_id: laser_turn_on_refused}
      - action: notify.mobile_app_pixel_9_pro_xl
        continue_on_error: true
        data: {title: "Laser turn-on refused", message: "{{ msg }}", data: {tag: laser-turn-on, channel: "Shop safety"}}
```

HA answers 200 to every webhook call, refused or not, so the hub's `ok` only means HA answered. pstation confirms the
turn-on from the next plug readings and otherwise tells the member what HA needs.

## After installing

- `/admin/lasers` reads "Home Assistant: reporting" within a minute (the 30 s push), and the state line follows the
  plug.
- Testing Turn on needs a person at the laser and the operator's go-ahead, and comes after the "before enabling Turn
  on" checks (pstation `docs/laser-control-plan-2026-10-14.md` and the 2026-10-14 contract, section 7).
