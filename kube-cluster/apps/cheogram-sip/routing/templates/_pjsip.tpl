{{/*
The host's DEFAULT registration: the `entrypoint` one, or the only one. It alone carries the
type=identify. "" when undecided, which tests/check-routing.py rejects. A disabled entry is
not on the host.
*/}}
{{- define "cheogram-sip-routing.pjsip-default" -}}
{{- $names := list -}}
{{- $entry := list -}}
{{- range $n, $r := .regs -}}
{{- if and (eq $r.host $.host) (not $r.disabled) -}}
{{- $names = append $names $n -}}
{{- if $r.entrypoint -}}{{- $entry = append $entry $n -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- if eq (len $entry) 1 -}}{{ index $entry 0 }}
{{- else if and (eq (len $entry) 0) (eq (len $names) 1) -}}{{ index $names 0 }}
{{- end -}}
{{- end -}}

{{- define "cheogram-sip-routing.pjsip" -}}
; GENERATED from the cheogram-sip-routing chart's values. Do not edit; edit values.yaml.
;
; One object set per registration, named after the ENTRY: a registration is the finest thing
; pjsip can attribute an inbound call to. The name is the dialplan's `peer` and what
; CHANNEL(endpoint) returns. The passwords are #included from files render-config writes
; from the Secret key of the same name; livekit and matrixbridge are appended from
; files/pjsip-in-cluster.conf.
{{- range $t := .Values.transports }}

[{{ $t.name }}]
type=transport
protocol={{ $t.protocol }}
bind={{ $t.bind }}
external_media_address={{ $.Values.external_address }}
external_signaling_address={{ $.Values.external_address }}
{{- range $net := $.Values.local_nets }}
local_net={{ $net }}
{{- end }}
{{- end }}
{{- $regs := .Values.registrations }}
{{- $transport := .Values.trunk_transport }}
{{- range $name, $r := $regs }}
{{- /* Disabled: no object at all, so nothing REGISTERs, answers or #includes a secret. */}}
{{- if $r.disabled }}{{ continue }}{{ end }}
{{- $default := include "cheogram-sip-routing.pjsip-default" (dict "host" $r.host "regs" $regs) }}
{{- $isdefault := eq $default $name }}

[{{ $name }}]
type=auth
auth_type=userpass
username={{ $r.user }}
; render-config derives the Secret key by stripping -secret.conf.
#include {{ $name }}-secret.conf

[{{ $name }}]
type=aor
; Where PJSIP/<exten>@<endpoint> is sent. The qualify also holds the NAT pinhole open.
contact=sip:{{ $r.host }}
qualify_frequency=60

[{{ $name }}]
type=registration
transport={{ $transport }}
outbound_auth={{ $name }}
server_uri=sip:{{ $r.host }}
client_uri=sip:{{ $r.user }}@{{ $r.host }}
; What the carrier echoes in the inbound Request-URI: which of our numbers was dialled.
; An "@" would make the registration object fail to load.
contact_user={{ $r.user }}
; Short, so the inbound NAT pinhole is not closed for most of the default hour.
expiration=120
retry_interval=20
; By default a 401/403 is permanent, so a credential problem would register once and go
; silent.
max_retries=10000
auth_rejection_permanent=no
forbidden_retry_interval=300
fatal_retry_interval=300
; Tags our Contact with ;line= and matches a call echoing it back to THIS registration.
line=yes
endpoint={{ $name }}
{{- if $isdefault }}

[{{ $name }}]
type=identify
endpoint={{ $name }}
; Exactly one per host: two would match the same address. Resolved at config load, so it
; pins the carrier's proxy until the next reload.
match={{ $r.host }}
{{- end }}

[{{ $name }}]
type=endpoint
transport={{ $transport }}
aors={{ $name }}
outbound_auth={{ $name }}
{{- if $isdefault }}
; Explicit: the default has moved between Asterisk versions. Only the host's default
; registration may be `ip`; the others are reachable by their own ;line= only.
identify_by=ip
{{- else }}
identify_by=username
{{- end }}
; pjsip has no [general]: without these an inbound call lands in `default`, which
; extensions.lua does not define, and an out-of-call MESSAGE goes to `public`.
context=public
message_context=messages-in
; The presented From; the dialplan sets no caller ID on a trunk leg.
from_user={{ $r.user }}
from_domain={{ $r.host }}
; NAT traversal, per endpoint.
force_rport=yes
rtp_symmetric=yes
direct_media=no
disallow=all
allow=alaw,ulaw
; rfc4733, not rfc2833: the old spelling is not accepted.
dtmf_mode=rfc4733
{{- end }}

{{ tpl (.Files.Get "files/pjsip-in-cluster.conf") . }}
{{- end -}}
