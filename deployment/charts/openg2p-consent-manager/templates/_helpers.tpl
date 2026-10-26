{{/*
Service account name for the API component.
*/}}
{{- define "consentManagerApi.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "common.names.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{/*
Render the env: list from .Values.envVars (literal/templated) and
.Values.envVarsFrom (valueFrom blocks). Inner Helm templates are resolved
against the root context ($).
*/}}
{{/*
Agri Stack exchange env — rendered only when its settings are set, so a
standalone install's manifests are unchanged. Call with the root context.
*/}}
{{/*
The Agri Stack exchange role: "exchange", "department", or "" (none, or the
fields as set). Root context.
*/}}
{{- define "consentManager.exchangeRole" -}}
{{- $r := (.Values.global.agriStackExchange | default dict).role | default "" -}}
{{- if has $r (list "exchange" "department") }}{{ $r }}{{ end -}}
{{- end -}}

{{/*
This install's domain: the CM partner hostname without its first label, e.g.
trial.openg2p.org. Root context.
*/}}
{{- define "consentManager.ownDomain" -}}
{{- (splitn "." 2 (tpl .Values.global.consentManagerPartnerHostname .))._1 -}}
{{- end -}}

{{- define "consentManager.exchangeEnv" -}}
{{- $x := .Values.global.agriStackExchange | default dict -}}
{{- $role := include "consentManager.exchangeRole" . -}}
{{- $presenters := $x.receiptPresenters | default list -}}
{{- if kindIs "string" $presenters }}{{ $presenters = compact (splitList "," (nospace $presenters)) }}{{ end -}}
{{- $issuer := $x.issuer | default "" -}}
{{- if eq $role "exchange" }}
{{- $issuer = $issuer | default $x.defaultIssuer -}}
{{- if not $presenters }}{{ $presenters = list $x.defaultPresenter }}{{ end -}}
{{- end -}}
{{- $issuers := list -}}
{{- range ($x.trustedReceiptIssuers | default list) }}{{ $issuers = append $issuers . }}{{ end -}}
{{- $t := $x.trustedIssuer | default dict -}}
{{- if eq $role "department" }}
{{- $domain := $x.exchangeDomain | default (printf "agrix.%s" (splitn "." 2 (include "consentManager.ownDomain" .))._1) -}}
{{- $jwks := $t.jwksUrl | default (printf "https://consent-manager-partner.%s/api/consent-manager-partner/.well-known/jwks.json" $domain) -}}
{{- $issuers = append $issuers (dict "issuer" ($t.issuer | default $x.defaultIssuer) "jwks_url" $jwks "presenter" ($t.presenter | default $x.defaultPresenter)) -}}
{{- else if $t.issuer }}
{{- $issuers = append $issuers (dict "issuer" $t.issuer "jwks_url" $t.jwksUrl "presenter" ($t.presenter | default "")) -}}
{{- end -}}
{{- if and $issuer $presenters }}
- name: CONSENT_MANAGER_RECEIPT_ISSUER
  value: {{ tpl $issuer . | quote }}
- name: CONSENT_MANAGER_RECEIPT_PRESENTERS
  value: {{ toJson $presenters | quote }}
- name: CONSENT_MANAGER_RECEIPT_TTL_SECONDS
  value: {{ $x.receiptTtlSeconds | default 900 | quote }}
{{- end }}
{{- if $issuers }}
- name: CONSENT_MANAGER_TRUSTED_RECEIPT_ISSUERS
  value: {{ toJson $issuers | quote }}
- name: CONSENT_MANAGER_RECEIPT_STATUS_CHECK
  value: {{ $x.receiptStatusCheck | default "always" | quote }}
{{- end }}
{{- end -}}

{{/*
Partner portal env for the STAFF api (it serves /consent/v1/partner-portal):
the partner realm's issuer + JWKS (partnerPortal.enabled) and evidence storage
(evidence.enabled or partnerPortal.enabled). Empty otherwise, so a standalone
install's manifests are unchanged. Call with the root context.
*/}}
{{- define "consentManager.partnerPortalEnv" -}}
{{- $p := .Values.partnerPortal | default dict -}}
{{- $e := .Values.evidence | default dict -}}
{{- if $p.enabled }}
- name: CONSENT_MANAGER_PARTNER_AUTH_ISSUER
  value: {{ tpl $p.keycloak.issuerUrl . | quote }}
- name: CONSENT_MANAGER_PARTNER_AUTH_JWKS_URL
  value: {{ tpl $p.keycloak.jwksUrl . | quote }}
- name: CONSENT_MANAGER_CONSENT_RECORD_JWKS_URL
  value: {{ tpl $p.consentRecordJwksUrl . | quote }}
- name: CONSENT_MANAGER_SUBJECT_REQUIRED_IDENTIFIERS
  {{- $ids := $p.requiredSubjectIdentifiers | default list }}
  {{- if kindIs "string" $ids }}{{ $ids = compact (splitList "," (nospace $ids)) }}{{ end }}
  {{- if and (not $ids) (eq (include "consentManager.exchangeRole" .) "exchange") }}{{ $ids = .Values.global.agriStackExchange.defaultSubjectIdentifiers | default list }}{{ end }}
  value: {{ $ids | toJson | quote }}
{{- $sa := $p.subjectAuth | default dict }}
{{- if include "consentManager.subjectAuthOn" . }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_ENABLED
  value: "true"
- name: CONSENT_MANAGER_SUBJECT_AUTH_PROVIDER_NAME
  value: {{ $sa.providerName | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_DISCOVERY_URL
  value: {{ tpl $sa.discoveryUrl . | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_CLIENT_ID
  value: {{ tpl $sa.clientId . | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_REDIRECT_URI
  value: {{ tpl $sa.redirectUri . | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_ACR_VALUES
  value: {{ $sa.acrValues | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_ID_TYPES
  value: {{ $sa.idTypes | toJson | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_PRIVATE_KEY_PATH
  value: {{ printf "%s/client.pem" $sa.keyMountPath | quote }}
{{- end }}
{{- end }}
{{- if or $e.enabled $p.enabled }}
- name: CONSENT_MANAGER_EVIDENCE_S3_ENDPOINT
  value: {{ tpl $e.s3.endpoint . | quote }}
- name: CONSENT_MANAGER_EVIDENCE_S3_BUCKET
  value: {{ tpl $e.s3.bucket . | quote }}
- name: CONSENT_MANAGER_EVIDENCE_S3_REGION
  value: {{ tpl $e.s3.region . | quote }}
- name: CONSENT_MANAGER_EVIDENCE_MAX_BYTES
  value: {{ $e.maxBytes | int64 | toString | quote }}
- name: CONSENT_MANAGER_EVIDENCE_S3_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ tpl $e.s3.existingSecret . | quote }}
      key: {{ $e.s3.accessKeyKey | quote }}
- name: CONSENT_MANAGER_EVIDENCE_S3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ tpl $e.s3.existingSecret . | quote }}
      key: {{ $e.s3.secretKeyKey | quote }}
{{- end }}
{{- end -}}

{{/*
true when the staff api mounts the subject-auth client key (partner portal +
national-ID login on; always on for the exchange role). Root context.
*/}}
{{- define "consentManager.subjectAuthOn" -}}
{{- $p := .Values.partnerPortal | default dict -}}
{{- $exchange := eq (include "consentManager.exchangeRole" .) "exchange" -}}
{{- if and $p.enabled (or ($p.subjectAuth | default dict).enabled $exchange) }}true{{ end -}}
{{- end -}}

{{/*
true when beneficiary API auth (national ID, eSignet) is on (beneApiAuth.enabled).
Root context.
*/}}
{{- define "consentManager.beneAuthOn" -}}
{{- if (.Values.beneApiAuth | default dict).enabled }}true{{ end -}}
{{- end -}}

{{/*
true when the consent login (national ID for one consent request, eSignet) is
on (consentLogin.enabled). Separate from the beneficiary API auth. Root context.
*/}}
{{- define "consentManager.consentLoginOn" -}}
{{- if (.Values.consentLogin | default dict).enabled }}true{{ end -}}
{{- end -}}

{{/*
true when the beneficiary api has a national-ID login: the beneficiary API auth,
the consent login, or both. Root context.
*/}}
{{- define "consentManager.nationalIdLoginOn" -}}
{{- if or (include "consentManager.beneAuthOn" .) (include "consentManager.consentLoginOn" .) }}true{{ end -}}
{{- end -}}

{{/*
true when the eSignet client (partnerPortal.subjectAuth) is needed: by the
partner portal's national-ID step, the beneficiary API auth, the consent login.
Root context.
*/}}
{{- define "consentManager.esignetClientOn" -}}
{{- if or (include "consentManager.subjectAuthOn" .) (include "consentManager.nationalIdLoginOn" .) }}true{{ end -}}
{{- end -}}

{{/*
The redirect URIs registered for the eSignet client, as JSON. Root context.
*/}}
{{- define "consentManager.esignetRedirectUris" -}}
{{- $uris := list -}}
{{- if include "consentManager.subjectAuthOn" . }}{{ $uris = append $uris (tpl .Values.partnerPortal.subjectAuth.redirectUri .) }}{{ end -}}
{{- if include "consentManager.beneAuthOn" . }}{{ $uris = append $uris (tpl .Values.beneApiAuth.redirectUri .) }}{{ end -}}
{{- if include "consentManager.consentLoginOn" . }}{{ $uris = append $uris (tpl .Values.consentLogin.redirectUri .) }}{{ end -}}
{{- toJson $uris -}}
{{- end -}}

{{/*
true when an extraApis entry is deployed: enabled, or the beneficiary api with
a national-ID login on (beneficiary API auth and/or consent login). Call with (dict "api" <entry> "root" $).
*/}}
{{- define "consentManager.extraApiOn" -}}
{{- if or .api.enabled (and (eq (toString .api.apiAudience) "beneficiary") (include "consentManager.nationalIdLoginOn" .root)) }}true{{ end -}}
{{- end -}}

{{/*
National-ID login env for the BENEFICIARY api: the eSignet client of
partnerPortal.subjectAuth (its redirect URIs are the bene API's callbacks), then
the beneficiary API auth (beneApiAuth) and the consent login (consentLogin)
settings, each when on. Empty when both are off. Root context.
*/}}
{{- define "consentManager.beneAuthEnv" -}}
{{- if include "consentManager.nationalIdLoginOn" . }}
{{- $sa := .Values.partnerPortal.subjectAuth -}}
- name: CONSENT_MANAGER_SUBJECT_AUTH_ENABLED
  value: "true"
- name: CONSENT_MANAGER_SUBJECT_AUTH_PROVIDER_NAME
  value: {{ $sa.providerName | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_DISCOVERY_URL
  value: {{ tpl $sa.discoveryUrl . | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_CLIENT_ID
  value: {{ tpl $sa.clientId . | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_ACR_VALUES
  value: {{ $sa.acrValues | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_ID_TYPES
  value: {{ $sa.idTypes | toJson | quote }}
- name: CONSENT_MANAGER_SUBJECT_AUTH_PRIVATE_KEY_PATH
  value: {{ printf "%s/client.pem" $sa.keyMountPath | quote }}
{{- end }}
{{- if include "consentManager.beneAuthOn" . }}
{{- $b := .Values.beneApiAuth -}}
{{- $returnUrls := list -}}
{{- range $b.allowedReturnUrls }}{{ $returnUrls = append $returnUrls (tpl . $) }}{{ end }}
- name: CONSENT_MANAGER_BENE_AUTH_ENABLED
  value: "true"
- name: CONSENT_MANAGER_BENE_AUTH_REDIRECT_URI
  value: {{ tpl $b.redirectUri . | quote }}
- name: CONSENT_MANAGER_BENE_AUTH_ALLOWED_RETURN_URLS
  value: {{ $returnUrls | toJson | quote }}
- name: CONSENT_MANAGER_BENE_AUTH_TOKEN_TTL_SECONDS
  value: {{ $b.tokenTtlSeconds | quote }}
- name: CONSENT_MANAGER_BENE_AUTH_EXCHANGE_TTL_SECONDS
  value: {{ $b.exchangeTtlSeconds | quote }}
- name: CONSENT_MANAGER_BENE_AUTH_SUBJECT_ID_TYPE
  value: {{ $b.subjectIdType | quote }}
{{- end }}
{{- if include "consentManager.consentLoginOn" . }}
{{- $c := .Values.consentLogin -}}
{{- $returnUrls := list -}}
{{- range $c.allowedReturnUrls }}{{ $returnUrls = append $returnUrls (tpl . $) }}{{ end }}
- name: CONSENT_MANAGER_CONSENT_LOGIN_ENABLED
  value: "true"
- name: CONSENT_MANAGER_CONSENT_LOGIN_REDIRECT_URI
  value: {{ tpl $c.redirectUri . | quote }}
- name: CONSENT_MANAGER_CONSENT_LOGIN_ALLOWED_RETURN_URLS
  value: {{ $returnUrls | toJson | quote }}
- name: CONSENT_MANAGER_CONSENT_LOGIN_TOKEN_TTL_SECONDS
  value: {{ $c.tokenTtlSeconds | quote }}
- name: CONSENT_MANAGER_CONSENT_LOGIN_EXCHANGE_TTL_SECONDS
  value: {{ $c.exchangeTtlSeconds | quote }}
- name: CONSENT_MANAGER_CONSENT_LOGIN_SUBJECT_ID_TYPE
  value: {{ $c.subjectIdType | quote }}
{{- end }}
{{- end -}}

{{- define "consentManagerApi.envVars" -}}
{{- range $key, $value := .Values.envVars }}
- name: {{ $key }}
  value: {{ tpl (printf "%v" $value) $ | quote }}
{{- end }}
{{- range $key, $spec := .Values.envVarsFrom }}
- name: {{ $key }}
  valueFrom:
{{ tpl (toYaml $spec) $ | indent 4 }}
{{- end }}
{{- end -}}

{{/*
Sanity Keycloak client-credentials env (the consent-manager admin client). Used
for CM admin calls and as the fallback token for PM admin seeding when no
dedicated partner_manager client is configured.
*/}}
{{- define "consentManagerSanity.kcClientEnv" -}}
- name: SANITY_TOKEN_URL
  value: "{{ tpl .Values.global.keycloakIssuerUrl $ }}/protocol/openid-connect/token"
- name: SANITY_CLIENT_ID
  value: {{ tpl .Values.global.consentManagerAuthClientId $ | quote }}
- name: SANITY_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ tpl .Values.global.consentManagerAuthClientId $ }}
      key: client_secret
      optional: true
{{- end -}}

{{/*
Partner Management seed env — shared by the pm-seed Job (deploy-time seeding)
and the sanity Job (its idempotent safety-net check). The signing private key
is bundled in the sanity image (TEST only); PM stores the derived public half.
*/}}
{{- define "consentManagerSanity.pmSeedEnv" -}}
- name: SANITY_VERIFY_TLS
  value: {{ .Values.sanity.verifyTls | quote }}
- name: SANITY_PM_PARTNER_API_URL
  value: {{ tpl .Values.global.partnerManagementApiUrl $ | quote }}
- name: SANITY_PM_ADMIN_URL
  value: {{ tpl .Values.global.partnerManagementAdminApiUrl $ | quote }}
# Authenticate to PM's admin API AS PM's own admin client (holds partner_manager).
- name: SANITY_PM_ADMIN_TOKEN_URL
  value: "{{ tpl .Values.global.keycloakIssuerUrl $ }}/protocol/openid-connect/token"
- name: SANITY_PM_ADMIN_CLIENT_ID
  value: {{ tpl .Values.global.pmSeedClientId $ | quote }}
- name: SANITY_PM_ADMIN_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ tpl .Values.global.pmSeedClientId $ | quote }}
      key: client_secret
      optional: true
{{- end -}}

{{/*
URL AWE posts policy-change decisions to (root context). global.aweCallbackUrl
if set; else the STAFF api's in-cluster Service — named exactly as
templates/api/service.yaml names it, so it follows consentManagerApi.nameOverride
(e.g. `cm-api` under commons-services).
*/}}
{{- define "consentManager.aweCallbackUrl" -}}
{{- if .Values.global.aweCallbackUrl -}}
{{- tpl .Values.global.aweCallbackUrl . -}}
{{- else -}}
{{- $staff := dict "Values" (merge (deepCopy .Values.consentManagerApi) (dict "global" .Values.global)) "Chart" .Chart "Release" .Release -}}
{{- $port := int (default 80 .Values.consentManagerApi.service.port) -}}
http://{{ include "common.names.fullname" $staff }}{{ if ne $port 80 }}:{{ $port }}{{ end }}/consent/v1/awe/webhooks/decision
{{- end -}}
{{- end -}}

{{/*
Whether a hook-annotations map still marks a Helm hook (an umbrella chart may
null the keys out to run the resource as a plain one). Returns "true" or "".
*/}}
{{- define "consentManager.isHook" -}}
{{- if and . (index . "helm.sh/hook") -}}true{{- end -}}
{{- end -}}
