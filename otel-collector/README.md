# otel-collector

Collettore telemetrico non autoritativo (§13.1): redazione, batch ed export OTLP. Solo endpoint di segnale; non può chiamare l'API comandi del controller né alterare l'evidenza (§15.1).

Deployable isolato del baseline IADF: privilegio e failure isolation ne giustificano l'esistenza (ADD §15.2). I ruoli agent sono configurazioni, non servizi.
