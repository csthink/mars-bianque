{{/*
Resource names are fixed rather than prefixed with the release name: Alertmanager in the
demo cluster sends alerts to http://bianque-backend.bianque.svc:8000.
*/}}

{{- define "bianque.labels" -}}
app.kubernetes.io/name: bianque
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{- define "bianque.selector" -}}
app.kubernetes.io/name: bianque
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "bianque.image" -}}
{{ printf "%s:%s" .repository .tag }}
{{- end }}

{{/*
Settings shared by the backend and the knowledge import Job: the database, and the
embedding model built into the image.
*/}}
{{- define "bianque.databaseEnv" -}}
- name: BIANQUE_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.database }}
      key: password
- name: BIANQUE_DATABASE_URL
  value: postgresql+psycopg://postgres:$(BIANQUE_DB_PASSWORD)@bianque-db:5432/postgres
- name: BIANQUE_EMBEDDING_MODE
  value: local
{{- end }}

{{/*
The backend runs as the image's unprivileged user, on a read-only root file system.
*/}}
{{- define "bianque.backendSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
capabilities:
  drop: [ALL]
seccompProfile:
  type: RuntimeDefault
{{- end }}
