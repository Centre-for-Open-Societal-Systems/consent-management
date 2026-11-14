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
  value: {{ $x.receiptTtlSeconds | default 300 | quote }}
{{- end }}
{{- $templates := tpl ($x.consentTemplateSourceUrl | default "") . -}}
{{- if and (not $templates) (eq $role "exchange") }}{{ $templates = $x.defaultConsentTemplateSourceUrl }}{{ end -}}
{{- if $templates }}
- name: CONSENT_MANAGER_CONSENT_TEMPLATE_SOURCE_URL
  value: {{ $templates | quote }}
{{- end }}
{{- if $issuers }}
- name: CONSENT_MANAGER_TRUSTED_RECEIPT_ISSUERS
  value: {{ toJson $issuers | quote }}
- name: CONSENT_MANAGER_RECEIPT_STATUS_CHECK
  value: {{ $x.receiptStatusCheck | default "never" | quote }}
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
Master Data code lists (partner portal purposes and subject ID types) for the
STAFF api, which serves the partner portal API. Root context.
*/}}
{{- define "consentManager.masterDataEnv" -}}
{{- $m := .Values.masterData | default dict -}}
- name: CONSENT_MANAGER_MDS_URL
  value: {{ tpl ($m.url | default "") . | quote }}
- name: CONSENT_MANAGER_MDS_TOKEN_URL
  value: "{{ tpl .Values.global.keycloakIssuerUrl . }}/protocol/openid-connect/token"
- name: CONSENT_MANAGER_MDS_CLIENT_ID
  value: {{ tpl .Values.global.consentManagerAuthClientId . | quote }}
# Its secret: the keycloak-init Secret of the same name.
- name: CONSENT_MANAGER_MDS_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ tpl .Values.global.consentManagerAuthClientId . | quote }}
      key: client_secret
      optional: true
- name: CONSENT_MANAGER_MDS_PURPOSE_LIST_CODE
  value: {{ $m.purposeListCode | default "" | quote }}
- name: CONSENT_MANAGER_MDS_SUBJECT_ID_TYPE_LIST_CODE
  value: {{ $m.subjectIdTypeListCode | default "" | quote }}
- name: CONSENT_MANAGER_MDS_CACHE_TTL
  value: {{ $m.cacheTtl | default 300 | quote }}
- name: CONSENT_MANAGER_MDS_TIMEOUT
  value: {{ $m.timeout | default 5 | quote }}
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
Consent receipt withdrawal (root context): the method text, and the
beneficiary api's base URL when that extra api is enabled.
*/}}
{{- define "consentManager.receiptWithdrawalEnv" -}}
- name: CONSENT_MANAGER_RECEIPT_WITHDRAWAL_METHOD
  value: {{ .Values.global.consentReceiptWithdrawalMethod | default "" | quote }}
{{- range .Values.extraApis }}
{{- if and .enabled (eq (toString .apiAudience) "beneficiary") }}
- name: CONSENT_MANAGER_RECEIPT_WITHDRAWAL_SUBJECT_API_URL
  value: {{ printf "https://%s%s" (tpl (toString .hostname) $) (.openapiRootPath | default "") | quote }}
{{- end }}
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
Partner Management seed env — shared by the pm-seed Job (deploy-time seeding
of the sanity partner, its key and its policy) and the sanity Job (its
idempotent safety-net check). The signing private key
is bundled in the sanity image (TEST only); PM stores the derived public half.
*/}}
{{- define "consentManagerSanity.pmSeedEnv" -}}
- name: SANITY_VERIFY_TLS
  value: {{ .Values.sanity.verifyTls | quote }}
- name: SANITY_PM_PARTNER_API_URL
  value: {{ tpl .Values.global.partnerManagementApiUrl $ | quote }}
- name: SANITY_PM_ADMIN_URL
  value: {{ tpl .Values.global.partnerManagementAdminApiUrl $ | quote }}
# The sanity partner's PM policy is for this data controller.
- name: SANITY_CONTROLLER_ID
  value: {{ .Values.sanity.controllerId | quote }}
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

