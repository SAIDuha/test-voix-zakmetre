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
                      f"?output_format=mp3_44100_128",
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
    t0 = time.time()
    derniere = None
    for modele in [m for m in MODELES_SCRIBE if m]:
        limite = "----zakmetre" + uuid.uuid4().hex
        champs = [("model_id", modele), ("language_code", "fr"), ("tag_audio_events", "false")]
        corps = b"".join(f"--{limite}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in champs)
        corps += (f"--{limite}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"micro.wav\"\r\n"
                  f"Content-Type: audio/wav\r\n\r\n").encode() + audio + f"\r\n--{limite}--\r\n".encode()
        req = urllib.request.Request("https://api.elevenlabs.io/v1/speech-to-text", data=corps, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={limite}", "xi-api-key": CLE_ELEVEN})
        try:
            r = urllib.request.urlopen(req, timeout=30)
            d = json.loads(r.read().decode("utf-8"))
            return jsonify(texte=str(d.get("text") or "").strip()[:500], t_ms=int((time.time() - t0) * 1000), modele=modele)
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
