# iadf-controller

Unica autorità di transizione di stato e policy (§13.1): rischio, budget, timer, outbox transazionale, emissione comandi side-effect. Singleton attivo con standby e leader lease; il crash riprende dallo stato canonico in PostgreSQL senza split brain (§15.1).

Deployable isolato del baseline IADF: privilegio e failure isolation ne giustificano l'esistenza (ADD §15.2). I ruoli agent sono configurazioni, non servizi.
