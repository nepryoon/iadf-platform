# iadf-worker

Pool effimeri non fidati (§13.1): context/index (`iadf-worker-context`), task agent in sandbox (`iadf-worker-agent`), verifica deterministica e firma receipt (`iadf-worker-verify`). Nessuna autorità operativa; kill immediato su violazione di policy (§15.1).

Deployable isolato del baseline IADF: privilegio e failure isolation ne giustificano l'esistenza (ADD §15.2). I ruoli agent sono configurazioni, non servizi.
