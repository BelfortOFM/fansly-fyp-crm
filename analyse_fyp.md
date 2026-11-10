# Analyse de l'algorithme FYP (For You Page) — Fansly

Source : `ss-prod-ew1-ns.aws.adobess.com_09-17-2026-16-19-27.har` (210 Mo, capture Charles Proxy).

## Compte observé
- Analyse faite depuis un compte de test (identifiants retires de cette copie).

---

## 1. Les endpoints impliqués dans la FYP

| Endpoint | Méthode | Rôle |
|---|---|---|
| `/api/v1/contentdiscovery/media/suggestionsnew` | GET | **Le feed FYP** (paginé par curseur, `limit=10`) |
| `/api/v1/it/fyp` | POST | Rapport d'événements d'interaction FYP (1 événement par post vu) |
| `/api/v1/it/mois` | POST | Impressions **fines** des "media offers" (temps de visionnage, scroll) |
| `/api/v1/it/pis` | POST | Impressions **page/profil/post** (entrées/sorties avec durée) |
| `/api/v1/contentdiscovery/suggestions` | GET | Suggestions de créateurs (8 profils) |
| `/api/v1/contentdiscovery/livesuggestions` | GET | Suggestions de lives |
| `wss://wsv3.fansly.com/?v=3` | WS | Temps réel (session/notifications) |

`it` = "interaction/impression tracking".

---

## 2. Récupération du feed

```
GET /api/v1/contentdiscovery/media/suggestionsnew?before=0&after=<curseur>&tagIds=&limit=10&offset=0
```

- `after` = curseur (id du dernier élément vu), `before` = 0, `limit` = 10 par page.
- `tagIds` = vide ici (pas de filtre tag).

### Réponse
```json
{
  "success": true,
  "response": {
    "mediaOfferSuggestions": [ ... ],   // liste ordonnée = le classement
    "aggregationData": {                // données dénormalisées pour le rendu
      "accounts":           [ ... ],    // profils créateurs
      "accountMedia":       [ ... ],    // médias + variantes + likeCount + price
      "accountMediaBundles":[ ... ],
      "posts":              [ ... ],
      "stories":            [ ... ],
      "tipGoals":           [ ... ],
      "tips":               [ ... ]
    }
  }
}
```

### Structure d'un `mediaOfferSuggestion`
```json
{
  "id": "956972899879641088",      // id interne de la suggestion
  "accountId": "1",                // "1" = feed global/défaut (nouveau compte)
  "mediaOfferId": "953019521730363392", // MOID = identifiant de TRACKING
  "mediaOfferType": 2001,          // 2001 = post simple, 2003 = bundle
  "mediaOfferBundleId": null,
  "mediaId": "953019067629842432", // contenu réel
  "mediaType": 2,                  // 1 = image, 2 = vidéo
  "previewId": "953019067629842432",
  "correlationId": "953019523605229571", // unique par suggestion (anti-déduplication)
  "tags": [],                      // tags "contenu" auto (vide ici)
  "postTags": [ ... ],             // tags posés par le créateur
  "valid": true
}
```

**Important** : le **classement est 100 % serveur**. Aucun champ `score`/`rank`/`weight`/`boost` n'est renvoyé au client — le client reçoit juste une liste déjà ordonnée.

---

## 3. Les signaux de tracking (le cœur de l'algorithme)

### 3.1 `POST /api/v1/it/mois` — Media Offer Impressions
Le client remonte pour **chaque media offer** le temps passé à l'écran, à granularité fine.

```json
{ "IS": [
  { "MOID": "953019521730363392",
    "MOBID": null,
    "MOA": 1,            // Media Offer Action (0/1)
    "LT": null,          // Like Type (null si pas de like)
    "LID": null,         // Like ID
    "PID": "953019067629842432",  // Post ID
    "SCI": 1,            // 1 = entrée dans le viewport, 0 = heartbeat suivant
    "SP": 0,
    "ITI": 1244          // Impression Time Interval (ms) — temps à l'écran
  } ]
}
```

Séquence typique pour un même MOID : SCI=1 puis plusieurs SCI=0 avec des ITI croissants (~1000/2000/3000 ms). → **dwell time** (temps de visionnage) mesuré en continu.

### 3.2 `POST /api/v1/it/fyp` — événements FYP
Un événement par post affiché :

```json
{ "IS": [
  { "SID": null, "T": 0,
    "MOID": "928027750281797632",   // media offer id
    "MID":  "865698874621181952",   // media id
    "MOBID": null,
    "IT": 7453,                      // temps total de visionnage (ms)
    "AT": 0, "TSI": 0, "TSIIT": 0,
    "IM": "{\"TIDS\":[],\"TID\":null,\"IES\":{\"T\":44001,\"CID\":\"928027750281797632\",\"TS\":1789654659626}}"
  } ]
}
```

`IM.IES.T = 44001` = événement "vu/ouvert" (même code que `pis`). La réponse est `{"success":true,"response":[]}` : c'est un endpoint de **collecte**, pas de renvoi de contenu.

### 3.3 `POST /api/v1/it/pis` — Page/Profile/Post Impressions

| Champ | Signification |
|---|---|
| `T` | type d'événement |
| `PAID` | Profile/Post Author ID |
| `IT` | durée (ms) |
| `TSI` | compteur de sessions |
| `TSIIT` | temps cumulé |
| `LC` | last change (timestamp) |

Types observés :
- `T=10001` : **entrée** page profil (IT=0)
- `T=10000` : **sortie** page profil (IT = durée sur le profil)
- `T=44001` : **ouverture/expansion** d'un post (IT=0)
- `T=44000` : **fermeture** d'un post (IT = durée de lecture)

---

## 4. Le schéma de l'algorithme (reconstitué)

```
            ┌──────────────────────────────────────────────┐
            │  1. FETCH   suggestionsnew (limit=10, curseur)│
            │             → liste ordonnée côté serveur     │
            └──────────────────────┬───────────────────────┘
                                   ▼
            ┌──────────────────────────────────────────────┐
            │  2. RENDER   le client affiche les media      │
            │              offers (vidéos surtout)          │
            └──────────────────────┬───────────────────────┘
                                   ▼
            ┌──────────────────────────────────────────────┐
            │  3. TRACK   le client mesure en continu :     │
            │   • dwell time (it/mois → ITI)               │
            │   • événements vu/ouvert (it/fyp → IES.T)    │
            │   • profils/posts ouverts+fermés (it/pis)    │
            │   • likes (media/{id}/likes)                 │
            │   • follows, subs, etc.                      │
            └──────────────────────┬───────────────────────┘
                                   ▼
            ┌──────────────────────────────────────────────┐
            │  4. RE-RANK   le serveur agrège ces signaux   │
            │   (engagement = f(dwell time, likes, tags)    │
            │   pour classer les prochaines pages           │
            └──────────────────────────────────────────────┘
```

### Signaux de classement probables (déduits)
1. **Dwell time / watch time** — mesuré finement (ITI/IT), signal dominant.
2. **Tags** — `postTags` (posés par le créateur, avec `viewCount`/`postCount` globaux) + `tags` (détection contenu). Ex : `petite`, `teen`, `blonde`, `asian`, `milf`, `fyp`…
3. **Engagement social** — `likeCount` (visible dans `accountMedia`), follows, subscriptions.
4. **Popularité créateur** — `followCount`, `timelineStats` (imageCount/videoCount).
5. **Boucle d'apprentissage** — les événements `it/*` nourrissent un profil utilisateur (tags vus, durées, likes) qui affine le ranking.

### Comportement "nouveau compte"
- `accountId: "1"` sur les media offers = **feed global/trending** (aucune personnalisation).
- Le feed est quasi **100 % vidéo** (`mediaType: 2`) : c'est le format poussé par défaut.

---

## 5. Auth & anti-bot (nécessaire pour rejouer l'API)

- **Authorization** : `base64(accountId:1:2:<token>)`
  ex. décodé → `<sessionId>:1:2:<token>` (sessionId, pas accountId)
- **Cookies** : `f-s-c` (session cryptée), `f-d` / `fansly-d` (device id), `fansly-ts-info` (offset horloge serveur pour détecter triche de temps), `f-v-v` (version app).
- **Headers custom** : `fansly-client-id`, `fansly-session-id`, `fansly-client-check`, `fansly-client-ts` (timestamp anti-replay).
- Le `fansly-ts-info` (offset serveur) + `fansly-client-ts` servent à détecter toute manipulation des durées d'impression → le dwell time est protégé.

---

## 6. Points clés à retenir

1. **La FYP = `contentdiscovery/media/suggestionsnew`** (curseur `after`, `limit=10`).
2. **Le classement n'est jamais exposé** au client (aucun score) : impossible de "voir" le score depuis la capture, seul l'ordre final est visible.
3. **Le tracking d'engagement est le vrai moteur** : `it/mois` (dwell time fin), `it/fyp` (événements "vu"), `it/pis` (profils/posts ouverts), likes.
4. **`MOID` (mediaOfferId) est l'identifiant de tracking** qui relie la suggestion servie aux impressions remontées — c'est la clé de la boucle de feedback.
5. **Tags** (`postTags` créateur + `tags` auto) sont un signal majeur de personnalisation.
6. **Nouveaux comptes** → feed global vidéo (`accountId=1`), personnalisation qui démarre dès les premiers signaux.
