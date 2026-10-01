{{/*
pjsip.conf, generated from the same registrations table as routing.lua. Nothing per-trunk is
re-encoded here: every value below is the entry's own, or a deployment fact from values.yaml.

The name of the host's DEFAULT registration -- the `entrypoint` one, or the only one on the
host. It alone carries the type=identify, and "" when the table leaves that undecided, which
tests/check-routing.py is what rejects. A disabled entry is not on the host at all.
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
; The two in-cluster peers are appended verbatim from files/pjsip-in-cluster.conf; see there.
;
; ONE OBJECT SET PER REGISTRATION, named after the ENTRY -- not after the host. Three DIDs
; share sip.example.net and a registration is the finest thing pjsip can attribute an inbound call
; to, so a set per host could carry only one of them. That name is load-bearing twice:
; routing.lua's `peer`, and what CHANNEL(endpoint) hands the dialplan.
;
; Only each `password=` line is rendered, by the render-config initContainer, from the same
; table. Adding a DID is an entry there plus a Secret key of the same name.
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
; Written by render-config under exactly this name, the entry's peer_secret_file.
#include {{ $r.peer_secret_file }}

[{{ $name }}]
type=aor
; Where PJSIP/<exten>@<endpoint> is actually sent; the registration object supplies no
; address. The qualify is also what holds the NAT pinhole open.
contact=sip:{{ $r.host }}
qualify_frequency=60

[{{ $name }}]
type=registration
transport={{ $transport }}
outbound_auth={{ $name }}
server_uri=sip:{{ $r.host }}
client_uri=sip:{{ $r.user }}@{{ $r.host }}
; What the carrier echoes in the inbound Request-URI, and therefore which of our numbers was
; dialled. It MUST be a legal SIP user part: pjsip parses the Contact URI it builds, so an
; "@" here makes the whole registration object fail to load -- one sorcery error and silence.
contact_user={{ $r.contact_user }}
; Two minutes: the default of an hour would leave the pinhole inbound arrives down closed
; for most of it.
expiration=120
retry_interval=20
; By default pjsip gives up after max_retries and treats a 401/403 as PERMANENT, so a
; credential problem would register once and then go silent.
max_retries=10000
auth_rejection_permanent=no
forbidden_retry_interval=300
fatal_retry_interval=300
; Tags our Contact with a generated ;line= and matches a call echoing it back to THIS
; registration -- the only per-DID discriminator pjsip has of its own.
line=yes
endpoint={{ $name }}
{{- if $isdefault }}

[{{ $name }}]
type=identify
endpoint={{ $name }}
; EXACTLY ONE PER HOST, on its default registration: two would match the same address and
; which endpoint answered would be undefined. Resolved at config load, so it pins the
; carrier's proxy until the next reload.
match={{ $r.host }}
{{- end }}

[{{ $name }}]
type=endpoint
transport={{ $transport }}
aors={{ $name }}
outbound_auth={{ $name }}
{{- if $isdefault }}
; EXPLICIT, because the default has moved between Asterisk versions and an implicit value is
; how a trunk silently starts matching on IP alone. Only the host's default registration may
; be `ip`; the others are reachable by their own ;line= and nothing else.
identify_by=ip
{{- else }}
identify_by=username
{{- end }}
; pjsip has no [general]. Without a context an inbound call lands in `default`, which
; extensions.lua does not define; out-of-call MESSAGE goes to `context` unless pointed at the
; dispatcher, and public's Message branch rings nobody.
context=public
message_context=messages-in
; The presented From. Nothing else pins it -- the dialplan does not set caller ID on a
; trunk leg.
from_user={{ $r.user }}
from_domain={{ $r.host }}
; NAT traversal, per endpoint: there is no [general] to state it once.
force_rport=yes
rtp_symmetric=yes
direct_media=no
disallow=all
allow=alaw,ulaw
; rfc4733, NOT rfc2833: the old spelling is not accepted and the endpoint would be left on
; the default.
dtmf_mode=rfc4733
{{- end }}

{{ .Files.Get "files/pjsip-in-cluster.conf" }}
{{- end -}}
