"""TEST VOIX — page séparée de ZAKMETRE (rien n'est modifié dans ZAKMETRE).

1. Écoute : la même phrase lue par plusieurs voix (ElevenLabs, Gemini, OpenAI si clé), avec le temps.
2. Chrono : « appuyer pour parler » -> Gemini comprend et répond -> la voix choisie lit la réponse.
   La page affiche : temps pour comprendre + répondre, temps avant le 1er son.

Lancer :  python serveur.py   puis ouvrir http://127.0.0.1:5050
Les clés sont dans le fichier .env (à côté de ce fichier). Elles ne partent jamais vers le navigateur.
"""
import base64
import json
import uuid
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, request, send_from_directory

ICI = os.path.dirname(os.path.abspath(__file__))


def charger_env():
    chemin = os.path.join(ICI, ".env")
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    k, v = ligne.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


charger_env()
CLE_GEMINI = os.environ.get("GEMINI_API_KEY", "").strip()
CLE_ELEVEN = os.environ.get("ELEVENLABS_API_KEY", "").strip()
CLE_OPENAI = os.environ.get("OPENAI_API_KEY", "").strip()
MODELE_GEMINI = os.environ.get("GEMINI_MODEL", "gemini-flash-latest").strip()

app = Flask(__name__)

# En ligne (Render) : la page est protégée par un mot de passe (variable MOT_DE_PASSE), sinon n'importe qui
# trouvant l'adresse utiliserait tes clés. Sur ton PC (sans MOT_DE_PASSE), rien ne change.
MOT_DE_PASSE = os.environ.get("MOT_DE_PASSE", "").strip()


@app.before_request
def _proteger():
    if not MOT_DE_PASSE:
        return None
    if request.path.startswith("/relais/") or request.path == "/tel":
        return None      # relais iPhone <-> ZAKMETRE : protégé par ses propres clés (voir plus bas), sans mot de passe
    a = request.authorization
    if a and a.password == MOT_DE_PASSE:
        return None
    return Response("Mot de passe demandé", 401, {"WWW-Authenticate": 'Basic realm="Test voix ZAKMETRE"'})


class Erreur(Exception):
    pass


def appel(url, corps=None, entetes=None, methode="POST", delai=40):
    data = json.dumps(corps).encode("utf-8") if corps is not None else None
    req = urllib.request.Request(url, data=data, method=methode,
                                 headers={"Content-Type": "application/json", **(entetes or {})})
    try:
        return urllib.request.urlopen(req, timeout=delai)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        for cle in (CLE_GEMINI, CLE_ELEVEN, CLE_OPENAI):   # jamais une clé dans un message
            if cle:
                detail = detail.replace(cle, "***")
        raise Erreur(f"le service a refusé ({e.code}) : {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Erreur(f"pas de connexion au service ({type(e).__name__})") from None


# ------------------------------------------------------------------ voix
VOIX_GEMINI = ["Kore", "Aoede", "Charon", "Puck", "Leda", "Orus"]
MODELES_TTS_GEMINI = [m for m in (os.environ.get("GEMINI_TTS_MODEL", "").strip(), "gemini-3.1-flash-tts-preview",
                                  "gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts") if m]
VOIX_OPENAI = ["coral", "sage", "nova", "ash", "verse", "alloy"]
MODELES_ELEVEN = [("eleven_v4_turbo", "v4 Turbo (conversation, expressif)"),
                  ("eleven_flash_v2_5", "Flash 2.5 (le plus rapide)"),
                  ("eleven_v4", "v4 (la plus belle, plus lente)")]


def wav(pcm, rate=24000):
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


_GEMINI_TTS_OK = {}


def voix_gemini(texte, voix):
    """Même façon de faire que ZAKNOTES : on essaie les modèles connus et les 2 façons d'écrire la voix."""
    essais = []
    modeles = [_GEMINI_TTS_OK["m"]] if _GEMINI_TTS_OK else MODELES_TTS_GEMINI
    for m in modeles:
        for forme in ("voice", "prebuilt"):
            essais.append((m, forme))
    derniere = None
    for m, forme in essais:
        vc = {"voiceConfig": {"voice": voix}} if forme == "voice" else {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voix}}}
        corps = {"contents": [{"role": "user", "parts": [{"text": texte}]}],
                 "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": vc}}
        try:
            r = appel(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", corps,
                      {"x-goog-api-key": CLE_GEMINI})
            d = json.loads(r.read().decode("utf-8"))
            for p in d["candidates"][0]["content"]["parts"]:
                x = p.get("inlineData") or p.get("inline_data")
                if x and x.get("data"):
                    brut = base64.b64decode(x["data"])
                    _GEMINI_TTS_OK["m"] = m
                    if brut[:4] == b"RIFF":
                        return brut, "audio/wav"
                    rate = re.search(r"rate=(\d+)", x.get("mimeType") or x.get("mime_type") or "")
                    return wav(brut, int(rate.group(1)) if rate else 24000), "audio/wav"
            derniere = "réponse sans son"
        except Erreur as e:
            derniere = str(e)
        except (KeyError, IndexError, ValueError):
            derniere = "réponse illisible"
    raise Erreur(f"voix Gemini impossible : {derniere}")


@app.get("/api/voix")
def api_voix():
    """Lit un texte avec une voix. ElevenLabs et OpenAI arrivent EN FLUX : le son commence avant la fin."""
    texte = (request.args.get("texte") or "").strip()[:600]
    fournisseur = request.args.get("f", "")
    voix = request.args.get("voix", "")
    modele = request.args.get("modele", "")
    if not texte:
        return jsonify(erreur="Texte vide."), 400
    try:
        if fournisseur == "eleven":
            if not CLE_ELEVEN:
                return jsonify(erreur="Pas de clé ElevenLabs dans .env"), 400
            r = appel(f"https://api.elevenlabs.io/v1/text-to-speech/{urllib.parse.quote(voix)}/stream"
                      f"?output_format=mp3_22050_32",
                      {"text": texte, "model_id": modele or "eleven_flash_v2_5"}, {"xi-api-key": CLE_ELEVEN})
            return Response(_flux(r), mimetype="audio/mpeg", headers={"Cache-Control": "no-store"})
        if fournisseur == "openai":
            if not CLE_OPENAI:
                return jsonify(erreur="Pas de clé OpenAI dans .env"), 400
            r = appel("https://api.openai.com/v1/audio/speech",
                      {"model": modele or "gpt-4o-mini-tts", "voice": voix or "coral", "input": texte,
                       "response_format": "mp3", "instructions": "Parle en français, ton naturel, professionnel et chaleureux."},
                      {"Authorization": f"Bearer {CLE_OPENAI}"})
            return Response(_flux(r), mimetype="audio/mpeg", headers={"Cache-Control": "no-store"})
        if fournisseur == "gemini":
            if not CLE_GEMINI:
                return jsonify(erreur="Pas de clé Gemini dans .env"), 400
            son, mime = voix_gemini(texte, voix or "Kore")
            return Response(son, mimetype=mime, headers={"Cache-Control": "no-store"})
        return jsonify(erreur="Fournisseur inconnu."), 400
    except Erreur as e:
        return jsonify(erreur=str(e)), 502


def _flux(r):
    with r:
        while True:
            morceau = r.read(4096)
            if not morceau:
                break
            yield morceau


@app.get("/api/voix_eleven")
def api_voix_eleven():
    """Les voix ElevenLabs de ton compte (celles ajoutées depuis la bibliothèque, en français, y compris)."""
    if not CLE_ELEVEN:
        return jsonify(voix=[], erreur="Pas de clé ElevenLabs dans .env")
    try:
        r = appel("https://api.elevenlabs.io/v1/voices", None, {"xi-api-key": CLE_ELEVEN}, methode="GET")
        d = json.loads(r.read().decode("utf-8"))
        voix = [{"id": v.get("voice_id"), "nom": v.get("name"),
                 "info": " · ".join(x for x in ((v.get("labels") or {}).get("accent"), (v.get("labels") or {}).get("gender"),
                                                 (v.get("labels") or {}).get("language")) if x)}
                for v in d.get("voices", [])]
        return jsonify(voix=voix)
    except Erreur as e:
        return jsonify(voix=[], erreur=str(e))


@app.get("/api/etat")
def api_etat():
    return jsonify(gemini=bool(CLE_GEMINI), eleven=bool(CLE_ELEVEN), openai=bool(CLE_OPENAI),
                   voix_gemini=VOIX_GEMINI, voix_openai=VOIX_OPENAI, modeles_eleven=MODELES_ELEVEN)


# ------------------------------------------------------------------ « appuyer pour parler » : Gemini comprend et répond
CONSIGNE = """Tu es Zak, l'assistant vocal de ZAKMETRE, un outil qui fait les DPGF et les CCTP d'un maître d'œuvre
du bâtiment à partir des plans. Tu parles français, tu tutoies, tu réponds en 1 ou 2 phrases courtes et naturelles
(elles seront lues à voix haute : pas de liste, pas de symbole). C'est un TEST : tu ne fais aucune action pour de vrai,
mais réponds comme si tu allais la faire et pose la question utile suivante (par exemple pour un nouveau chantier :
maître d'œuvre ou contractant général ? EMOP ou L&A ?).
Écris d'abord, mot pour mot, ce que la personne a dit dans « entendu ». N'INVENTE JAMAIS : n'écris que les mots que
tu entends clairement (une simple salutation reste une salutation). Si l'enregistrement est vide, inaudible ou n'est
que du bruit, mets "entendu": "" et "reponse": "".
Réponds en JSON : {"entendu": "…", "reponse": "…"}"""

CONSIGNE_TEXTE = """Tu es Zak, l'assistant vocal de ZAKMETRE, un outil qui fait les DPGF et les CCTP d'un maître d'œuvre
du bâtiment à partir des plans. Tu parles français, tu tutoies, tu réponds en 1 ou 2 phrases courtes et naturelles
(elles seront lues à voix haute : pas de liste, pas de symbole). C'est un TEST : tu ne fais aucune action pour de vrai,
mais réponds comme si tu allais la faire et pose la question utile suivante (par exemple pour un nouveau chantier :
maître d'œuvre ou contractant général ? EMOP ou L&A ?). Réponds seulement à ce que la personne a dit, sans rien
supposer de plus. Réponds en JSON : {"reponse": "…"}

La personne a dit : """

_REFLEXION = [{"thinkingLevel": "minimal"}, {"thinkingBudget": 0}, None]
_BON_REGLAGE = {}      # modèle -> réglage de réflexion accepté : on ne refait pas l'essai (et l'attente) à chaque phrase


def _reglages():
    r = _BON_REGLAGE.get(MODELE_GEMINI, "?")
    return [r] if r != "?" else _REFLEXION


VOCABULAIRE = ["Zak", "ZAKMETRE", "maître d'œuvre", "contractant général", "EMOP", "L&A", "DPGF", "CCTP",
               "plan de repérage", "lot peinture", "chantier", "Hautepierre", "Strasbourg", "Vilogia", "NinjaStorm",
               "maître d'ouvrage", "bureau de contrôle", "coordonnateur SPS", "DCE", "indice", "finition"]
MODELES_SCRIBE = [os.environ.get("SCRIBE_MODEL", "").strip(), "scribe_v3", "scribe_v2"]


@app.post("/api/transcrire")
def api_transcrire():
    """Le son du micro -> texte, par ElevenLabs Scribe (un vrai outil de dictée : il écrit ce qu'il entend, il
    n'invente pas de phrase). Marche aussi sur un PC d'entreprise où la dictée de Chrome est bloquée."""
    if not CLE_ELEVEN:
        return jsonify(erreur="Pas de clé ElevenLabs"), 400
    audio = request.get_data(cache=False)
    if len(audio) < 3000:
        return jsonify(texte="", t_ms=0)
    # mots que la dictée doit bien reconnaître (« L&A » et pas « LER », « maître d'œuvre » et pas « médecin »)
    # + les noms du chantier en cours, envoyés par la page (en-tête X-Mots, séparés par « | »)
    extra = [m.strip()[:50] for m in urllib.parse.unquote(request.headers.get("X-Mots", "")).split("|") if m.strip()]
    mots = [m for m in dict.fromkeys(VOCABULAIRE + extra) if not re.search(r"[<>{}\[\]\\]", m)][:90]
    t0 = time.time()
    derniere = None
    essais = [(m, True) for m in MODELES_SCRIBE if m] + [(m, False) for m in MODELES_SCRIBE if m]
    for modele, avec_mots in essais:
        limite = "----zakmetre" + uuid.uuid4().hex
        champs = [("model_id", modele), ("language_code", "fr"), ("tag_audio_events", "false"), ("temperature", "0")]
        if avec_mots:
            champs += [("keyterms", m) for m in mots]
        corps = b"".join(f"--{limite}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in champs)
        corps += (f"--{limite}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"micro.wav\"\r\n"
                  f"Content-Type: audio/wav\r\n\r\n").encode() + audio + f"\r\n--{limite}--\r\n".encode()
        req = urllib.request.Request("https://api.elevenlabs.io/v1/speech-to-text", data=corps, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={limite}", "xi-api-key": CLE_ELEVEN})
        try:
            r = urllib.request.urlopen(req, timeout=30)
            d = json.loads(r.read().decode("utf-8"))
            return jsonify(texte=str(d.get("text") or "").strip()[:500], t_ms=int((time.time() - t0) * 1000), modele=modele,
                           vocabulaire=avec_mots)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300].replace(CLE_ELEVEN, "***")
            derniere = f"le service a refusé ({e.code}) : {detail}"
            if e.code in (400, 404, 422):          # modèle inconnu pour ce compte : le suivant
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            derniere = f"pas de connexion au service ({type(e).__name__})"
            break
    if derniere and ("401" in derniere or "missing_permissions" in derniere or "permission" in derniere.lower()):
        derniere += " — la clé ElevenLabs n'a pas le droit « Speech to Text » : modifie-la sur elevenlabs.io (Access)."
    return jsonify(erreur=f"Dictée impossible : {derniere}"), 502


CONSIGNE_ASSISTANT = """Tu es Zak, l'assistant vocal de ZAKMETRE (outil qui fait les DPGF et CCTP d'un maître d'œuvre du
bâtiment, sociétés EMOP et L&A). Tu parles français, tu tutoies, 1 ou 2 phrases courtes et naturelles (lues à voix
haute : pas de liste, pas de symbole, pas d'abréviation compliquée ; dis « L et A », « DPGF »).

TA MISSION DANS CE TEST : créer un nouveau chantier. Il te faut 4 informations :
  nom (le nom du chantier, ex. « Hôpital de Hautepierre »), ville, role, societe.
  role : "maitre_oeuvre" ou "contractant_general" (rien d'autre).
  societe : "EMOP" ou "L&A" (rien d'autre).
Demande UNE information à la fois, celle qui manque, en commençant par le nom.

LA DICTÉE PEUT SE TROMPER (le texte vient d'un micro) :
- corrige seulement si c'est évident d'après le contexte : « LER », « l'A », « L et A », « Léa » -> L&A ;
  « haute pierre », « hôpital de Pierre » quand on parle de Strasbourg -> Hautepierre ; « mettre d'œuvre » -> maître d'œuvre ;
- si tu corriges un NOM (chantier, ville), dis-le pour que la personne vérifie (« Hautepierre, c'est bien ça ? ») ;
- si la réponse ne correspond à AUCUNE option attendue (ex. tu attends maître d'œuvre ou contractant général et tu lis
  « médecin »), NE DEVINE PAS : ne remplis rien et redemande gentiment en rappelant les 2 choix ;
- n'invente jamais une information qui n'a pas été dite.

QUAND LES 4 INFORMATIONS SONT LÀ : résume en une phrase et demande confirmation (attend = "confirmation").
Si la personne confirme clairement (oui, c'est bon, vas-y, valide…) alors confirme = true et annonce que le chantier
est créé (c'est un test : rien n'est créé pour de vrai). Si elle corrige quelque chose, mets à jour et redemande confirmation.
Si elle parle d'autre chose, réponds brièvement puis reviens à l'information qui manque.

Réponds UNIQUEMENT en JSON :
{"reponse": "…", "fiche": {"nom": "…" ou null, "ville": "…" ou null, "role": "…" ou null, "societe": "…" ou null},
 "attend": "nom" | "ville" | "role" | "societe" | "confirmation" | "rien", "confirme": false}"""

ROLES = {"maitre_oeuvre", "contractant_general"}
SOCIETES = {"EMOP", "L&A"}


@app.post("/api/assistant")
def api_assistant():
    """Une étape de conversation : la page envoie la phrase, l'historique et la fiche ; Gemini répond, le serveur
    VÉRIFIE (rôle et société parmi les choix possibles, chantier « créé » seulement après un oui explicite)."""
    if not CLE_GEMINI:
        return jsonify(erreur="Pas de clé Gemini"), 400
    d = request.get_json(silent=True) or {}
    texte = str(d.get("texte") or "").strip()[:500]
    if not texte:
        return jsonify(erreur="Rien entendu"), 400
    fiche = {k: (str(v)[:80] if v else None) for k, v in (d.get("fiche") or {}).items() if k in ("nom", "ville", "role", "societe")}
    attend_avant = str(d.get("attend") or "nom")
    histo = [h for h in (d.get("historique") or []) if isinstance(h, dict)][-12:]
    tours = [{"role": "user" if h.get("qui") == "toi" else "model", "parts": [{"text": str(h.get("texte") or "")[:600]}]}
             for h in histo if h.get("texte")]
    etat = f"FICHE ACTUELLE : {json.dumps(fiche, ensure_ascii=False)} ; tu attendais : {attend_avant}.\nLa personne dit : {texte}"
    contenu = tours + [{"role": "user", "parts": [{"text": etat}]}]
    t0 = time.time()
    derniere = None
    for reflexion in _reglages():
        cfg = {"responseMimeType": "application/json", "temperature": 0.2}
        if reflexion:
            cfg["thinkingConfig"] = reflexion
        try:
            r = appel(f"https://generativelanguage.googleapis.com/v1beta/models/{MODELE_GEMINI}:generateContent",
                      {"systemInstruction": {"parts": [{"text": CONSIGNE_ASSISTANT}]}, "contents": contenu, "generationConfig": cfg},
                      {"x-goog-api-key": CLE_GEMINI}, delai=30)
            g = json.loads(r.read().decode("utf-8"))
            sortie = "".join(p.get("text", "") for p in g["candidates"][0]["content"]["parts"] if not p.get("thought"))
            m = re.search(r"\{.*\}", sortie, re.S)
            res = json.loads(m.group(0)) if m else {}
            _BON_REGLAGE[MODELE_GEMINI] = reflexion
            break
        except Erreur as e:
            derniere = str(e)
            if "400" in derniere and reflexion:
                continue
            return jsonify(erreur=f"Zak n'a pas pu répondre : {derniere}"), 502
        except (KeyError, IndexError, ValueError):
            return jsonify(erreur="Zak n'a pas pu répondre (réponse illisible)"), 502
    else:
        return jsonify(erreur=f"Zak n'a pas pu répondre : {derniere}"), 502
    # ---- contrôles : l'IA propose, le serveur dispose
    nf = dict(fiche)
    for k, v in (res.get("fiche") or {}).items():
        if k in ("nom", "ville") and v:
            nf[k] = str(v).strip()[:80]
        elif k == "role" and v in ROLES:
            nf[k] = v
        elif k == "societe" and v in SOCIETES:
            nf[k] = v
    manque = [k for k in ("nom", "ville", "role", "societe") if not nf.get(k)]
    attend = str(res.get("attend") or "")
    if manque and attend in ("confirmation", "rien", ""):
        attend = manque[0]
    cree = bool(res.get("confirme")) and attend_avant == "confirmation" and not manque
    if not manque:
        attend = "rien" if cree else "confirmation"      # jamais « créé » sans un oui juste après le résumé
    return jsonify(reponse=str(res.get("reponse") or "")[:600], fiche=nf, attend=attend, cree=cree,
                   t_ms=int((time.time() - t0) * 1000))


@app.get("/assistant")
def page_assistant():
    return send_from_directory(ICI, "assistant.html")


@app.post("/api/repondre")
def api_repondre():
    """Le navigateur (Chrome) a déjà écrit ce qui a été dit : Gemini n'a plus qu'à répondre au texte (plus rapide,
    et il ne peut plus « inventer » une phrase à partir d'un son mal capté)."""
    if not CLE_GEMINI:
        return jsonify(erreur="Pas de clé Gemini dans .env"), 400
    texte = str((request.get_json(silent=True) or {}).get("texte") or "").strip()[:500]
    if not texte:
        return jsonify(entendu="", reponse="", t_ms=0)
    t0 = time.time()
    derniere = None
    for reflexion in _reglages():
        cfg = {"responseMimeType": "application/json", "temperature": 0.4}
        if reflexion:
            cfg["thinkingConfig"] = reflexion
        try:
            r = appel(f"https://generativelanguage.googleapis.com/v1beta/models/{MODELE_GEMINI}:generateContent",
                      {"contents": [{"role": "user", "parts": [{"text": CONSIGNE_TEXTE + texte}]}], "generationConfig": cfg},
                      {"x-goog-api-key": CLE_GEMINI}, delai=30)
            d = json.loads(r.read().decode("utf-8"))
            sortie = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"] if not p.get("thought"))
            m = re.search(r"\{.*\}", sortie, re.S)
            res = json.loads(m.group(0)) if m else {}
            _BON_REGLAGE[MODELE_GEMINI] = reflexion
            return jsonify(entendu=texte, reponse=str(res.get("reponse") or "")[:600], t_ms=int((time.time() - t0) * 1000))
        except Erreur as e:
            derniere = str(e)
            if "400" in derniere and reflexion:
                continue
            break
        except (KeyError, IndexError, ValueError):
            derniere = "réponse illisible"
            break
    return jsonify(erreur=f"Gemini n'a pas répondu : {derniere}"), 502


@app.post("/api/parler")
def api_parler():
    if not CLE_GEMINI:
        return jsonify(erreur="Pas de clé Gemini dans .env"), 400
    audio = request.get_data(cache=False)
    if len(audio) < 3000:
        return jsonify(entendu="", reponse="", t_ms=0)
    t0 = time.time()
    parts = [{"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode("ascii")}},
             {"text": CONSIGNE}]
    derniere = None
    for reflexion in _reglages():     # « réfléchis le moins possible » : plus rapide
        cfg = {"responseMimeType": "application/json", "temperature": 0.0}
        if reflexion:
            cfg["thinkingConfig"] = reflexion
        try:
            r = appel(f"https://generativelanguage.googleapis.com/v1beta/models/{MODELE_GEMINI}:generateContent",
                      {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg},
                      {"x-goog-api-key": CLE_GEMINI}, delai=40)
            d = json.loads(r.read().decode("utf-8"))
            texte = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"] if not p.get("thought"))
            m = re.search(r"\{.*\}", texte, re.S)
            res = json.loads(m.group(0)) if m else {}
            return jsonify(entendu=str(res.get("entendu") or "")[:500], reponse=str(res.get("reponse") or "")[:600],
                           t_ms=int((time.time() - t0) * 1000))
        except Erreur as e:
            derniere = str(e)
            if "400" in derniere and reflexion:      # réglage de réflexion refusé par ce modèle : le suivant
                continue
            break
        except (KeyError, IndexError, ValueError):
            derniere = "réponse illisible"
            break
    return jsonify(erreur=f"Gemini n'a pas répondu : {derniere}"), 502


# ================================================================== RELAIS « micro sur téléphone »
# L'iPhone et ZAKMETRE (sur le PC) ne se parlent pas directement (pare-feu de l'entreprise) : les deux passent
# par ce serveur. Il ne fait que transmettre : aucune clé d'IA n'est utilisée ici, tout le travail se fait sur le PC.
#
# Sécurité :
#  - RELAIS_SECRET (même valeur ici et dans le .env de ZAKMETRE) ; sans lui, le relais est fermé ;
#  - chaque liaison a un identifiant aléatoire ; les clés sont des signatures du secret (rien à stocker : une
#    liaison survit au redémarrage du serveur) ; clé du PC et clé du téléphone sont différentes ;
#  - le téléphone obtient sa clé avec un code à usage unique, valable 10 minutes (affiché par ZAKMETRE) ;
#  - les essais de code faux sont limités.
import hashlib
import hmac
import threading

RELAIS_SECRET = os.environ.get("RELAIS_SECRET", "").strip()
_RELAIS = {}                 # id -> {"n": dernier n°, "msgs": [...], "reps": {n: réponse}, "pc_vu": t, "tel_vu": t, "vu": t}
_CODES = {}                  # code -> (id, expiration)
_RATES = []                  # heures des codes faux
_V = threading.Condition()
RE_ID = re.compile(r"[A-Za-z0-9_-]{16,40}")
RE_CODE = re.compile(r"[A-Z0-9]{6}")


def _signature(qui, sid):
    return hmac.new(RELAIS_SECRET.encode(), f"{qui}:{sid}".encode(), hashlib.sha256).hexdigest()


def _autorise(qui, sid):
    if not RELAIS_SECRET or not RE_ID.fullmatch(sid or ""):
        return False
    return hmac.compare_digest(request.headers.get("X-Cle", ""), _signature(qui, sid))


def _liaison(sid):
    """(appelée sous _V) la liaison, créée au besoin ; on oublie celles sans activité depuis 2 h."""
    t = time.time()
    for k in [k for k, v in _RELAIS.items() if t - v["vu"] > 7200]:
        del _RELAIS[k]
    if sid not in _RELAIS:
        if len(_RELAIS) > 300:
            return None
        _RELAIS[sid] = {"n": 0, "msgs": [], "reps": {}, "pc_vu": 0, "tel_vu": 0, "vu": t, "liaisons": 0}
    _RELAIS[sid]["vu"] = t
    return _RELAIS[sid]


def _refus():
    return jsonify(erreur="Liaison refusée : rescanne le QR code affiché par ZAKMETRE."), 403


@app.post("/relais/code")
def relais_code():
    """ZAKMETRE dépose le code à usage unique qu'il affiche (avec le QR code)."""
    d = request.get_json(silent=True) or {}
    sid, code = str(d.get("id") or ""), str(d.get("code") or "")
    if not _autorise("pc", sid) or not RE_CODE.fullmatch(code):
        return _refus()
    with _V:
        t = time.time()
        for k in [k for k, v in _CODES.items() if v[1] < t or v[0] == sid]:
            del _CODES[k]
        _CODES[code] = (sid, t + 600)
        _liaison(sid)
    return jsonify(ok=True)


@app.post("/relais/appairer")
def relais_appairer():
    """Le téléphone échange le code contre sa clé (une seule fois)."""
    if not RELAIS_SECRET:
        return jsonify(erreur="Le relais n'est pas réglé (RELAIS_SECRET manquant sur Render)."), 503
    code = str((request.get_json(silent=True) or {}).get("code") or "").strip().upper().replace(" ", "")
    with _V:
        t = time.time()
        _RATES[:] = [x for x in _RATES if t - x < 600]
        if len(_RATES) >= 30:
            return jsonify(erreur="Trop d'essais. Attends quelques minutes."), 429
        v = _CODES.pop(code, None) if RE_CODE.fullmatch(code) else None
        if not v or v[1] < t:
            _RATES.append(t)
            return jsonify(erreur="Code inconnu ou expiré : affiche un nouveau QR code dans ZAKMETRE."), 403
        sid = v[0]
        l = _liaison(sid)
        if l:
            l["tel_vu"] = t
            l["liaisons"] += 1          # réveille ZAKMETRE : il affiche « téléphone relié » tout de suite
        _V.notify_all()
    return jsonify(id=sid, cle=_signature("tel", sid))


@app.post("/relais/<sid>/message")
def relais_message(sid):
    """Le téléphone envoie ce qu'il a dit (son WAV en base64) ou écrit."""
    if not _autorise("tel", sid):
        return _refus()
    d = request.get_json(silent=True) or {}
    audio, texte = str(d.get("audio") or ""), str(d.get("texte") or "").strip()[:500]
    if len(audio) > 3_000_000:
        return jsonify(erreur="Message trop long : 1 minute maximum."), 413
    if not audio and not texte:
        return jsonify(erreur="Message vide."), 400
    with _V:
        l = _liaison(sid)
        if l is None:
            return jsonify(erreur="Relais plein, réessaie dans un moment."), 503
        l["n"] += 1
        l["tel_vu"] = time.time()
        l["msgs"].append({"n": l["n"], "audio": audio, "texte": texte})
        l["msgs"] = l["msgs"][-5:]
        _V.notify_all()
        n = l["n"]
    return jsonify(n=n, pc_en_ligne=time.time() - l["pc_vu"] < 30)


@app.get("/relais/<sid>/attendre")
def relais_attendre(sid):
    """ZAKMETRE attend les messages du téléphone (réponse dès qu'il y en a un, sinon au bout de 20 s)."""
    if not _autorise("pc", sid):
        return _refus()
    try:
        apres = int(request.args.get("apres") or 0)
    except ValueError:
        apres = 0
    fin = time.time() + 20
    with _V:
        liaisons = (_liaison(sid) or {}).get("liaisons", 0)
        while True:
            l = _liaison(sid)
            if l is None:
                return jsonify(erreur="Relais plein."), 503
            l["pc_vu"] = time.time()
            nouveaux = [m for m in l["msgs"] if m["n"] > apres]
            reste = fin - time.time()
            if nouveaux or reste <= 0 or l["liaisons"] != liaisons:
                break
            _V.wait(min(reste, 5))
        # n le plus haut connu : au redémarrage du relais, ZAKMETRE repart de là sans rater de message
        return jsonify(messages=nouveaux, dernier=l["n"], tel_vu=l["tel_vu"])


@app.post("/relais/<sid>/reponse")
def relais_reponse_pc(sid):
    """ZAKMETRE renvoie la réponse de Zak (texte + voix en mp3 base64) pour le téléphone."""
    if not _autorise("pc", sid):
        return _refus()
    d = request.get_json(silent=True) or {}
    try:
        n = int(d.get("n"))
    except (TypeError, ValueError):
        return jsonify(erreur="Réponse sans numéro."), 400
    rep = {"entendu": str(d.get("entendu") or "")[:500], "texte": str(d.get("texte") or "")[:1500],
           "audio": str(d.get("audio") or "")[:2_000_000], "erreur": str(d.get("erreur") or "")[:300],
           "action": str(d.get("action") or "")[:200]}
    with _V:
        l = _liaison(sid)
        if l is None:
            return jsonify(erreur="Relais plein."), 503
        l["reps"][n] = rep
        for k in sorted(l["reps"])[:-5]:
            del l["reps"][k]
        _V.notify_all()
    return jsonify(ok=True)


@app.get("/relais/<sid>/reponse")
def relais_reponse_tel(sid):
    """Le téléphone attend la réponse à son message n (jusqu'à 25 s, puis il redemande)."""
    if not _autorise("tel", sid):
        return _refus()
    try:
        n = int(request.args.get("n") or 0)
    except ValueError:
        n = 0
    fin = time.time() + 25
    with _V:
        while True:
            l = _liaison(sid)
            if l is None:
                return jsonify(erreur="Relais plein."), 503
            l["tel_vu"] = time.time()
            rep = l["reps"].get(n)
            reste = fin - time.time()
            if rep or reste <= 0:
                break
            _V.wait(min(reste, 5))
        return jsonify(pret=bool(rep), **(rep or {}), pc_en_ligne=time.time() - l["pc_vu"] < 30)


@app.get("/relais/<sid>/etat")
def relais_etat(sid):
    if not _autorise("tel", sid):
        return _refus()
    with _V:
        l = _liaison(sid)
        if l is None:
            return jsonify(erreur="Relais plein."), 503
        l["tel_vu"] = time.time()
        return jsonify(pc_en_ligne=time.time() - l["pc_vu"] < 30)


@app.get("/tel")
def page_telephone():
    return send_from_directory(ICI, "telephone.html")


@app.get("/")
def page():
    return send_from_directory(ICI, "page.html")


if __name__ == "__main__":
    import sys
    print("Clés trouvées :", ", ".join(n for n, c in (("Gemini", CLE_GEMINI), ("ElevenLabs", CLE_ELEVEN),
                                                      ("OpenAI", CLE_OPENAI)) if c) or "aucune (remplissez .env)")
    if "telephone" in sys.argv:
        # mode téléphone : le serveur accepte les appareils du même Wi-Fi, en https (sinon l'iPhone refuse le micro)
        import socket
        try:
            s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s_.connect(("8.8.8.8", 80)); ip = s_.getsockname()[0]; s_.close()
        except OSError:
            ip = "ADRESSE-DU-PC"
        try:
            import cryptography  # noqa: F401  (nécessaire pour le https « maison »)
        except ImportError:
            print("\nIl manque un module pour le https. Tapez :  pip install cryptography   puis relancez.")
            sys.exit(1)
        print("\nMODE TÉLÉPHONE : sur l'iPhone (même Wi-Fi ou partage de connexion), ouvrez dans Safari :")
        print(f"    https://{ip}:5050")
        print("Safari dira « connexion non privée » : Afficher les détails > visiter ce site web. C'est normal : c'est votre PC.\n")
        app.run(host="0.0.0.0", port=5050, debug=False, threaded=True, ssl_context="adhoc")
    else:
        print("TEST VOIX : ouvrez http://127.0.0.1:5050 dans Chrome")
        app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
