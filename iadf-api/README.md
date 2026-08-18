# iadf-api

Intake, viste di lettura e comandi amministrativi pre-runtime (§13.1). Identità `svc-api`: tabelle/funzioni DB limitate; MAI merge, firma o deploy (§15.1). Idempotente, >=2 repliche quando la disponibilità lo richiede.

Deployable isolato del baseline IADF: privilegio e failure isolation ne giustificano l'esistenza (ADD §15.2). I ruoli agent sono configurazioni, non servizi.
