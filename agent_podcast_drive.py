import os
import re
import json
import time
import random
import shutil
import tempfile
import subprocess
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------
# CONFIGURATION VIA LES SECRETS GITHUB
# ---------------------------------------------------------
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "").strip()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
SERVICE_ACCOUNT_JSON = os.environ.get("GCP_CREDENTIALS_JSON", "").strip()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
EXTENSIONS_AUDIO = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")

MODELE_ANALYSE = "gemini-3.8-flash"
MODELE_TTS = "gemini-3.8-flash-tts"

# ---------------------------------------------------------
# CASTING VOCAL : VOIX VERROUILLÉES & POOLS ALÉATOIRES
# ---------------------------------------------------------
# Ajoutez ici en minuscules les hôtes récurrents dont la voix doit rester fixe
VOIX_VERROUILLEES = {
    "hannah fry": "Aoede",          # Hôte du Google DeepMind Podcast
    "professeur hannah fry": "Aoede",
    "professeure hannah fry": "Aoede",
    "professor hannah fry": "Aoede",
}

VOIX_HOMMES = [
    "Orus",      # Grave, posé
    "Fenrir",    # Dynamique, expressif
    "Charon",    # Chaleureux, profond
    "Puck"       # Vif, conversationnel
]

VOIX_FEMMES = [
    "Aoede",     # Claire, naturelle, expressive
    "Fola",      # Chaleureuse, posée
    "Kore",      # Dynamique, articulée
    "Leda"       # Douce, fluide
]

if not GOOGLE_API_KEY or not SERVICE_ACCOUNT_JSON or not DRIVE_FOLDER_ID:
    raise ValueError("Variables DRIVE_FOLDER_ID, GOOGLE_API_KEY ou GCP_CREDENTIALS_JSON manquantes.")

client_gemini = genai.Client(api_key=GOOGLE_API_KEY)

# ---------------------------------------------------------
# FONCTIONS GOOGLE DRIVE (CONNEXION FRAÎCHE & RETRY)
# ---------------------------------------------------------
def get_drive_service():
    """Crée une nouvelle instance Drive avec une connexion SSL fraîche."""
    info_compte = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(info_compte, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def lister_audios_drive(folder_id):
    service = get_drive_service()
    requete = f"'{folder_id}' in parents and trashed = false"
    resultats = service.files().list(
        q=requete,
        pageSize=100,
        fields="files(id, name, description)"
    ).execute()
    fichiers = resultats.get("files", [])
    audios = [f for f in fichiers if f["name"].lower().endswith(EXTENSIONS_AUDIO)]
    return sorted(audios, key=lambda x: x["name"])

def telecharger_fichier_drive_robuste(file_id, chemin_destination, max_essais=4):
    """Télécharge le fichier avec réinitialisation de la connexion SSL en cas de coupure."""
    for essai in range(1, max_essais + 1):
        try:
            service_frais = get_drive_service()
            request = service_frais.files().get_media(fileId=file_id)
            with open(chemin_destination, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=5 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return
        except Exception as err:
            if essai == max_essais:
                raise err
            delai = essai * 4
            print(f"     [Retry Drive {essai}/{max_essais}] Coupure réseau ({err}). Reconnexion dans {delai}s...")
            time.sleep(delai)

# ---------------------------------------------------------
# NETTOYAGE, AÉRATION & DÉCOUPAGE
# ---------------------------------------------------------
def aerer_et_structurer_texte(texte: str) -> str:
    if not texte:
        return ""
    texte = texte.replace("*", "").replace("_", "")
    texte = re.sub(r'\r\n', '\n', texte)
    texte = re.sub(r'\n{3,}', '\n\n', texte)
    texte = re.sub(r'[ \t]+', ' ', texte)
    return texte.strip()

def nettoyer_texte_pour_audio(texte: str) -> str:
    if not texte:
        return ""
    texte = re.sub(r'(\d+):(\d+)', r'\1, \2', texte)
    texte = texte.replace("*", "").replace("#", "").replace("_", "")
    texte = re.sub(r'\s*[-—–]\s*', ', ', texte)
    texte = texte.replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
    texte = re.sub(r'\s+([.,!?:;])', r'\1', texte)
    texte = re.sub(r'[ \t]+', ' ', texte)
    return texte.strip()

def decouper_texte_en_phrases(texte: str, max_chars: int = 1800) -> list:
    paragraphes = [p.strip() for p in texte.split("\n\n") if p.strip()]
    morceaux_finaux = []

    for para in paragraphes:
        if len(para) <= max_chars:
            morceaux_finaux.append(para)
        else:
            phrases = re.split(r'([.!?]+(?:\s+|$))', para)
            buffer = ""
            for part in phrases:
                if len(buffer) + len(part) <= max_chars:
                    buffer += part
                else:
                    if buffer.strip():
                        morceaux_finaux.append(buffer.strip())
                    buffer = part
            if buffer.strip():
                morceaux_finaux.append(buffer.strip())

    return morceaux_finaux

# ---------------------------------------------------------
# ÉTAPE 1 : TRANSCRIPTION & DIARISATION (VO)
# ---------------------------------------------------------
def attendre_fichier_actif(audio_file):
    while True:
        info_fichier = client_gemini.files.get(name=audio_file.name)
        etat = getattr(info_fichier.state, "name", str(info_fichier.state))
        if "ACTIVE" in etat:
            return info_fichier
        if "FAILED" in etat:
            raise RuntimeError(f"Échec du traitement interne du fichier audio par Google : {etat}")
        print(f"     [Attente indexation Google : {etat}] Nouvelle vérification dans 3s...")
        time.sleep(3)

def transcrire_et_diariser(chemin_audio: str, contexte: str):
    print("  -> Téléversement de la piste audio vers Gemini...")
    audio_file = None
    for essai in range(1, 4):
        try:
            audio_file = client_gemini.files.upload(file=chemin_audio)
            break
        except Exception as err:
            if essai == 3:
                raise err
            print(f"     [Retry Upload Gemini {essai}/3] {err}. Nouvel essai dans 5s...")
            time.sleep(5)

    audio_file = attendre_fichier_actif(audio_file)
    time.sleep(2)

    prompt = f"""
    Tu es un ingénieur du son et transcripteur professionnel.
    Voici le contexte du fichier audio :
    \"\"\"{contexte}\"\"\"

    CONSIGNES CRITIQUES :
    1. Transcris fidèlement l'intégralité de l'audio en découpant par tour de parole (diarisation).
    2. Identifie précisément chaque intervenant en t'aidant du contexte et de l'introduction orale (ex: "Hannah Fry", "Mark Cole", "Voix off", ou le nom complet de l'invité). Pour Hannah Fry, écris toujours exactement "Hannah Fry".
    3. Détermine le genre de chaque locuteur : strictement "masculin" ou "feminin".
    4. STRUCTURE ET AÉRATION : Si une prise de parole est longue, aère-la impérativement en plusieurs paragraphes distincts séparés par un double saut de ligne (\\n\\n). N'aplatis JAMAIS une longue intervention en un bloc compact continu.
    5. N'utilise AUCUN balisage Markdown (pas d'astérisques *, pas de dièses #).

    FORMAT DE SORTIE REQUIS (JSON pur uniquement) :
    [
      {{
        "speaker_id": "Speaker 1",
        "nom_locuteur": "Hannah Fry",
        "genre": "feminin",
        "texte_original": "Paroles prononcées en anglais aérées avec des \\n\\n..."
      }}
    ]
    """

    print(f"  -> Transcription et diarisation en cours ({MODELE_ANALYSE})...")
    try:
        reponse = client_gemini.models.generate_content(
            model=MODELE_ANALYSE,
            contents=[audio_file, prompt],
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json"
            )
        )
        donnees = json.loads(reponse.text)
    finally:
        if audio_file:
            try:
                client_gemini.files.delete(name=audio_file.name)
            except Exception:
                pass

    for seg in donnees:
        seg["texte_original"] = aerer_et_structurer_texte(seg.get("texte_original", ""))
    return donnees

# ---------------------------------------------------------
# ÉTAPE 2 : TRADUCTION STRUCTURÉE & AÉRÉE (VF)
# ---------------------------------------------------------
def traduire_dialogue(script_vo):
    print("  -> Traduction et adaptation stylistique en français...")
    prompt = f"""
    Tu es un adaptateur et traducteur littéraire pour le doublage audiovisuel.
    Traduis chaque réplique du dialogue suivant de l'anglais vers un français parlé fluide, naturel et élégant.

    CONSIGNES STRICTES DE STRUCTURE ET FORMATAGE :
    1. CONSERVE OU RENFORCE L'AÉRATION : chaque longue réplique doit être structurée en paragraphes aérés, séparés par un double saut de ligne (\\n\\n). N'aplatis JAMAIS le texte en un seul bloc indigeste.
    2. ZÉRO POLLUTION : Le champ 'texte_fr' doit contenir UNIQUEMENT les paroles prononcées par l'intervenant. Ne rajoute JAMAIS le nom de la personne, ni "Monsieur X :", ni introduction, ni conclusion.
    3. ZÉRO MARKDOWN : N'utilise AUCUN astérisque (* ou **), AUCUN dièse (#), AUCUN souligné (_).
    4. Références et chiffres : écris les références clairement sans symboles ambigus.

    DIALOGUE SOURCE :
    {json.dumps(script_vo, ensure_ascii=False)}

    FORMAT DE SORTIE REQUIS (JSON pur uniquement) :
    [
      {{
        "speaker_id": "Speaker 1",
        "nom_locuteur": "Hannah Fry",
        "genre": "feminin",
        "texte_original": "...",
        "texte_fr": "Premier paragraphe traduit.\\n\\nDeuxième paragraphe bien aéré..."
      }}
    ]
    """

    reponse = client_gemini.models.generate_content(
        model=MODELE_ANALYSE,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.25,
            response_mime_type="application/json"
        )
    )

    donnees = json.loads(reponse.text)
    for seg in donnees:
        seg["texte_fr"] = aerer_et_structurer_texte(seg.get("texte_fr", ""))
    return donnees

# ---------------------------------------------------------
# ATTRIBUTION DES VOIX (VERROUILLAGE HÔTE + ALÉATOIRE INVITÉS)
# ---------------------------------------------------------
def attribuer_voix_episode(script_vf) -> dict:
    """
    Verrouille la voix des hôtes définis dans VOIX_VERROUILLEES,
    puis attribue aléatoirement une voix distincte par genre aux invités.
    """
    map_locuteurs_voix = {}

    # 1. Identifier tous les locuteurs uniques de l'épisode
    locuteurs_uniques = []
    for seg in script_vf:
        nom = seg["nom_locuteur"].strip()
        genre = seg.get("genre", "masculin").lower()
        if nom not in [x[0] for x in locuteurs_uniques]:
            locuteurs_uniques.append((nom, genre))

    # 2. Verrouiller en priorité les voix des hôtes connus
    voix_deja_prises = set()
    for nom, genre in locuteurs_uniques:
        nom_normalise = nom.lower().strip()
        for cle_verrou, voix_fixe in VOIX_VERROUILLEES.items():
            if cle_verrou in nom_normalise:
                map_locuteurs_voix[nom] = voix_fixe
                voix_deja_prises.add(voix_fixe)
                break

    # 3. Préparer les réservoirs disponibles pour les invités (sans les voix verrouillées)
    dispo_hommes = [v for v in VOIX_HOMMES if v not in voix_deja_prises]
    dispo_femmes = [v for v in VOIX_FEMMES if v not in voix_deja_prises]

    # 4. Attribuer aléatoirement une voix aux invités selon leur sexe
    for nom, genre in locuteurs_uniques:
        if nom in map_locuteurs_voix:
            continue

        if "fem" in genre:
            if not dispo_femmes:
                dispo_femmes = [v for v in VOIX_FEMMES if v not in voix_deja_prises] or list(VOIX_FEMMES)
            voix_choisie = random.choice(dispo_femmes)
            dispo_femmes.remove(voix_choisie)
        else:
            if not dispo_hommes:
                dispo_hommes = [v for v in VOIX_HOMMES if v not in voix_deja_prises] or list(VOIX_HOMMES)
            voix_choisie = random.choice(dispo_hommes)
            dispo_hommes.remove(voix_choisie)

        map_locuteurs_voix[nom] = voix_choisie

    return map_locuteurs_voix

# ---------------------------------------------------------
# ÉTAPE 3 : SYNTHÈSE VOCALE GEMINI 3.8 FLASH TTS
# ---------------------------------------------------------
def convertir_flux_gemini_en_mp3(audio_bytes: bytes, mime_type: str, chemin_mp3_sortie: str, dossier_tmp: str, index_clip: int):
    est_wav = audio_bytes[:4] == b"RIFF" or "wav" in mime_type.lower()
    ext_brute = ".wav" if est_wav else ".pcm"
    chemin_brut = os.path.join(dossier_tmp, f"raw_{index_clip:05d}{ext_brute}")

    with open(chemin_brut, "wb") as f:
        f.write(audio_bytes)

    if est_wav:
        cmd = [
            "ffmpeg", "-y", "-i", chemin_brut,
            "-ar", "24000", "-ac", "1", "-b:a", "128k",
            chemin_mp3_sortie
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
            "-i", chemin_brut,
            "-b:a", "128k",
            chemin_mp3_sortie
        ]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    if os.path.exists(chemin_brut):
        os.remove(chemin_brut)

def synthese_segment_gemini_tts(
    segment: str,
    voix_principale: str,
    voix_secours: str,
    chemin_mp3_sortie: str,
    dossier_tmp: str,
    index_clip: int,
    max_essais: int = 5
) -> bool:
    for essai in range(1, max_essais + 1):
        voix_active = voix_principale if essai <= 3 else voix_secours
        try:
            reponse = client_gemini.models.generate_content(
                model=MODELE_TTS,
                contents=segment,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voix_active
                            )
                        )
                    )
                )
            )

            audio_bytes = None
            mime_type = "audio/pcm"
            if reponse.candidates and reponse.candidates[0].content and reponse.candidates[0].content.parts:
                for part in reponse.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        audio_bytes = part.inline_data.data
                        mime_type = part.inline_data.mime_type or "audio/pcm"
                        break

            if not audio_bytes:
                raise RuntimeError("Aucun flux audio retourné par le modèle TTS.")

            convertir_flux_gemini_en_mp3(audio_bytes, mime_type, chemin_mp3_sortie, dossier_tmp, index_clip)

            if os.path.exists(chemin_mp3_sortie) and os.path.getsize(chemin_mp3_sortie) > 0:
                return True

        except Exception as err:
            err_str = str(err)
            delai = 15 if ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str) else essai * 4
            print(f"     [Retry Gemini TTS {essai}/{max_essais} - Voix {voix_active}] Erreur ({err_str[:140]}). Attente {delai}s...")
            time.sleep(delai)

    return False

def orchestrer_synthese_gemini_tts(script_vf, chemin_mp3_final):
    map_locuteurs_voix = attribuer_voix_episode(script_vf)
    print(f"  -> Casting vocal attribué (Hôtes verrouillés + Invités aléatoires) : {map_locuteurs_voix}")

    dossier_tmp = tempfile.mkdtemp()
    try:
        chemin_silence = os.path.join(dossier_tmp, "silence.mp3")
        cmd_silence = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "0.35", "-b:a", "128k",
            chemin_silence
        ]
        subprocess.run(cmd_silence, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        fichiers_liste = []
        compteur_morceau = 0
        total_repliques = len(script_vf)

        for i, seg in enumerate(script_vf, start=1):
            texte_brut = seg["texte_fr"].strip()
            if not texte_brut:
                continue

            locuteur = seg["nom_locuteur"].strip()
            genre = seg.get("genre", "masculin").lower()
            voix_voulue = map_locuteurs_voix[locuteur]
            voix_secours = "Orus" if "fem" not in genre else "Aoede"

            print(f"  -> Synthèse réplique {i}/{total_repliques} ({locuteur} - Voix {voix_voulue})...")
            texte_purifie = nettoyer_texte_pour_audio(texte_brut)
            sous_morceaux = decouper_texte_en_phrases(texte_purifie, max_chars=1800)

            for sous_bloc in sous_morceaux:
                if not sous_bloc.strip():
                    continue
                clip_path = os.path.join(dossier_tmp, f"part_{compteur_morceau:05d}.mp3")
                succes = synthese_segment_gemini_tts(
                    segment=sous_bloc,
                    voix_principale=voix_voulue,
                    voix_secours=voix_secours,
                    chemin_mp3_sortie=clip_path,
                    dossier_tmp=dossier_tmp,
                    index_clip=compteur_morceau
                )
                if succes:
                    fichiers_liste.append(clip_path)
                    compteur_morceau += 1
                else:
                    print(f"     ⚠️ Échec définitif sur un sous-bloc de {locuteur}.")

                time.sleep(1.5)

            fichiers_liste.append(chemin_silence)

        if not fichiers_liste:
            raise RuntimeError("Aucun segment audio n'a pu être généré pour cet épisode.")

        concat_txt = os.path.join(dossier_tmp, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for item in fichiers_liste:
                f.write(f"file '{item}'\n")

        cmd_concat = [
            "ffmpeg", "-y", "-f", "concat",
            "-safe", "0", "-i", concat_txt,
            "-c", "copy", chemin_mp3_final
        ]
        subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    finally:
        shutil.rmtree(dossier_tmp, ignore_errors=True)

# ---------------------------------------------------------
# ORCHESTRATION GLOBALE RÉSILIENTE
# ---------------------------------------------------------
def executer_workflow():
    print(f"🎙️ Agent CastMorph démarré sur GitHub Actions (Moteur TTS : {MODELE_TTS}).")
    fichiers_audio = lister_audios_drive(DRIVE_FOLDER_ID)

    if not fichiers_audio:
        print("Aucun fichier audio trouvé dans le dossier Google Drive partagé.")
        return

    print(f"📂 {len(fichiers_audio)} fichier(s) audio détecté(s) dans Drive.\n")
    os.makedirs("output", exist_ok=True)
    dossier_telechargement = tempfile.mkdtemp()

    try:
        for f in fichiers_audio:
            nom_complet = f["name"]
            base_nom = nom_complet.rsplit(".", 1)[0]
            extension = os.path.splitext(nom_complet)[1]
            description_drive = f.get("description", "")

            chemin_vo = os.path.join("output", f"Transcription_VO_{base_nom}.txt")
            chemin_vf = os.path.join("output", f"Traduction_VF_{base_nom}.txt")
            chemin_mp3 = os.path.join("output", f"Podcast_VF_{base_nom}.mp3")

            if os.path.exists(chemin_vo) and os.path.exists(chemin_vf) and os.path.exists(chemin_mp3):
                print(f"⏭️ Déjà traité : {nom_complet}")
                continue

            print("==================================================")
            print(f"🎧 Traitement de : {nom_complet}")
            print("==================================================")

            chemin_local_source = os.path.join(dossier_telechargement, f"source_{f['id']}{extension}")

            try:
                telecharger_fichier_drive_robuste(f["id"], chemin_local_source)
                print("  ✓ Fichier téléchargé depuis Google Drive.")

                contexte = f"Nom du fichier : {base_nom}\nDescription : {description_drive}"
                script_vo = transcrire_et_diariser(chemin_local_source, contexte)

                texte_export_vo = ""
                for rep in script_vo:
                    texte_export_vo += f"[{rep['nom_locuteur']} ({rep['genre']})]\n{rep['texte_original']}\n\n" + ("-" * 40) + "\n\n"

                with open(chemin_vo, "w", encoding="utf-8") as f_vo:
                    f_vo.write(texte_export_vo.strip())
                print(f"  ✓ Transcription VO sauvegardée : Transcription_VO_{base_nom}.txt")

                script_vf = traduire_dialogue(script_vo)

                texte_export_vf = ""
                for rep in script_vf:
                    texte_export_vf += f"[{rep['nom_locuteur']}]\n{rep['texte_fr']}\n\n" + ("-" * 40) + "\n\n"

                with open(chemin_vf, "w", encoding="utf-8") as f_vf:
                    f_vf.write(texte_export_vf.strip())
                print(f"  ✓ Traduction VF sauvegardée : Traduction_VF_{base_nom}.txt")

                orchestrer_synthese_gemini_tts(script_vf, chemin_mp3)
                print(f"  ✓ Podcast doublé généré : Podcast_VF_{base_nom}.mp3\n")

            except Exception as err_fichier:
                print(f"  ❌ Erreur rencontrée sur « {nom_complet} » : {err_fichier}")
                print("  ⏭️ Passage au fichier suivant...\n")
            finally:
                if os.path.exists(chemin_local_source):
                    os.remove(chemin_local_source)

            time.sleep(3)

    finally:
        shutil.rmtree(dossier_telechargement, ignore_errors=True)
        print("🏁 Traitement de la file terminé.")

if __name__ == "__main__":
    executer_workflow()
