import os
import re
import sys
import json
import time
import signal
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
MODELE_TTS = "gemini-3.8-flash-lite-tts"

# ---------------------------------------------------------
# CASTING VOCAL (VOIX VERROUILLÉES & POOLS ALÉATOIRES)
# ---------------------------------------------------------
# Voix fixes pour les hôtes récurrents (clé en minuscules -> nom de voix Gemini)
VOIX_VERROUILLEES = {
    "hannah fry": "Aoede",
    "professor hannah fry": "Aoede",
    "professeure hannah fry": "Aoede",
    "professeur hannah fry": "Aoede",
}

VOIX_HOMMES = [
    "Orus",      # Grave, posé
    "Fenrir",    # Dynamique, expressif
    "Charon",    # Chaleureux, profond
    "Puck",      # Vif, conversationnel
]

VOIX_FEMMES = [
    "Aoede",     # Claire, naturelle, expressive
    "Fola",      # Chaleureuse, amicale, médium
    "Kore",      # Dynamique, articulée
    "Leda",      # Douce, fluide
]

if not GOOGLE_API_KEY or not SERVICE_ACCOUNT_JSON or not DRIVE_FOLDER_ID:
    raise ValueError("Variables DRIVE_FOLDER_ID, GOOGLE_API_KEY ou GCP_CREDENTIALS_JSON manquantes.")

client_gemini = genai.Client(api_key=GOOGLE_API_KEY)

# ---------------------------------------------------------
# GESTION DE L'ARRÊT MANUEL (SAUVEGARDE IMMÉDIATE)
# ---------------------------------------------------------
ARRET_DEMANDE = False

def gerer_signal_arret(signum, frame):
    global ARRET_DEMANDE
    ARRET_DEMANDE = True
    print("\n🛑 Arrêt manuel détecté ! Assemblage et sauvegarde des fichiers déjà générés...", flush=True)

signal.signal(signal.SIGINT, gerer_signal_arret)
signal.signal(signal.SIGTERM, gerer_signal_arret)

# ---------------------------------------------------------
# FONCTIONS GOOGLE DRIVE
# ---------------------------------------------------------
def get_drive_service():
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

def telecharger_fichier_drive_robuste(file_id, chemin_destination, max_essais=3):
    for essai in range(1, max_essais + 1):
        if ARRET_DEMANDE:
            return
        try:
            service_frais = get_drive_service()
            request = service_frais.files().get_media(fileId=file_id)
            with open(chemin_destination, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=5 * 1024 * 1024)
                done = False
                while not done:
                    if ARRET_DEMANDE:
                        return
                    _, done = downloader.next_chunk()
            return
        except Exception as err:
            if essai == max_essais:
                raise err
            time.sleep(essai * 3)

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
    while not ARRET_DEMANDE:
        info_fichier = client_gemini.files.get(name=audio_file.name)
        etat = getattr(info_fichier.state, "name", str(info_fichier.state))
        if "ACTIVE" in etat:
            return info_fichier
        if "FAILED" in etat:
            raise RuntimeError(f"Échec du traitement interne du fichier audio par Google : {etat}")
        print(f"     [Attente indexation Google : {etat}] Vérification dans 3s...", flush=True)
        time.sleep(3)
    return audio_file

def transcrire_et_diariser(chemin_audio: str, contexte: str):
    print("  -> Téléversement de la piste audio vers Gemini...", flush=True)
    audio_file = None
    for essai in range(1, 4):
        try:
            audio_file = client_gemini.files.upload(file=chemin_audio)
            break
        except Exception as err:
            if essai == 3:
                raise err
            time.sleep(4)

    audio_file = attendre_fichier_actif(audio_file)
    if ARRET_DEMANDE:
        return []

    prompt = f"""
    Tu es un ingénieur du son et transcripteur professionnel.
    Voici le contexte du fichier audio :
    \"\"\"{contexte}\"\"\"

    CONSIGNES CRITIQUES :
    1. Transcris fidèlement l'intégralité de l'audio en découpant par tour de parole (diarisation).
    2. Identifie précisément chaque intervenant en t'aidant du contexte et de l'introduction orale (ex: "Hannah Fry", "Voix off", ou le nom complet de l'invité). Pour Hannah Fry, écris toujours exactement "Hannah Fry".
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

    print(f"  -> Transcription et diarisation en cours ({MODELE_ANALYSE})...", flush=True)
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
    print("  -> Traduction et adaptation stylistique en français...", flush=True)
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
# CASTING VOCAL (HÔTES VERROUILLÉS + INVITÉS ALÉATOIRES)
# ---------------------------------------------------------
def attribuer_voix_episode(script_vf) -> dict:
    map_locuteurs_voix = {}
    locuteurs_uniques = []

    for seg in script_vf:
        nom = seg["nom_locuteur"].strip()
        genre = seg.get("genre", "masculin").lower()
        if nom not in [x[0] for x in locuteurs_uniques]:
            locuteurs_uniques.append((nom, genre))

    voix_deja_prises = set()

    # 1. Verrouillage des hôtes récurrents (ex: Hannah Fry -> Aoede)
    for nom, genre in locuteurs_uniques:
        nom_min = nom.lower().strip()
        for cle_verrou, voix_fixe in VOIX_VERROUILLEES.items():
            if cle_verrou in nom_min:
                map_locuteurs_voix[nom] = voix_fixe
                voix_deja_prises.add(voix_fixe)
                break

    # 2. Attribution aléatoire par genre pour les invités
    dispo_h = [v for v in VOIX_HOMMES if v not in voix_deja_prises]
    dispo_f = [v for v in VOIX_FEMMES if v not in voix_deja_prises]

    for nom, genre in locuteurs_uniques:
        if nom in map_locuteurs_voix:
            continue

        if "fem" in genre:
            if not dispo_f:
                dispo_f = [v for v in VOIX_FEMMES if v not in voix_deja_prises] or list(VOIX_FEMMES)
            voix_choisie = random.choice(dispo_f)
            dispo_f.remove(voix_choisie)
        else:
            if not dispo_h:
                dispo_h = [v for v in VOIX_HOMMES if v not in voix_deja_prises] or list(VOIX_HOMMES)
            voix_choisie = random.choice(dispo_h)
            dispo_h.remove(voix_choisie)

        map_locuteurs_voix[nom] = voix_choisie

    return map_locuteurs_voix

# ---------------------------------------------------------
# ÉTAPE 3 : SYNTHÈSE VOCALE GEMINI 3.8 FLASH LITE TTS (FAIL-FAST)
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

def assembler_fichiers_mp3(fichiers_liste: list, chemin_mp3_destination: str, dossier_tmp: str) -> bool:
    clips_valides = [f for f in fichiers_liste if os.path.exists(f) and os.path.getsize(f) > 0]
    if not clips_valides:
        return False

    concat_txt = os.path.join(dossier_tmp, "concat_list.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for item in clips_valides:
            f.write(f"file '{item}'\n")

    cmd_concat = [
        "ffmpeg", "-y", "-f", "concat",
        "-safe", "0", "-i", concat_txt,
        "-c", "copy", chemin_mp3_destination
    ]
    subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return os.path.exists(chemin_mp3_destination)

def synthese_segment_gemini_tts(
    segment: str,
    voix_name: str,
    chemin_mp3_sortie: str,
    dossier_tmp: str,
    index_clip: int,
    max_essais_reseau: int = 3
) -> bool:
    """
    Appelle gemini-3.8-flash-lite-tts avec SpeechConfig et arrêt immédiat (Fail-Fast)
    en cas d'erreur de code ou de paramètres API.
    """
    for essai in range(1, max_essais_reseau + 1):
        if ARRET_DEMANDE:
            return False
        try:
            reponse = client_gemini.models.generate_content(
                model=MODELE_TTS,
                contents=segment,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voix_name
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
                raise RuntimeError("Réponse vide : aucun flux audio retourné par gemini-3.8-flash-lite-tts.")

            convertir_flux_gemini_en_mp3(audio_bytes, mime_type, chemin_mp3_sortie, dossier_tmp, index_clip)
            return os.path.exists(chemin_mp3_sortie) and os.path.getsize(chemin_mp3_sortie) > 0

        except Exception as err:
            err_str = str(err)
            # Arrêt immédiat sans attente si erreur de code, paramètre, modèle ou authentification
            if any(code in err_str for code in ("400", "INVALID_ARGUMENT", "401", "403", "404", "NOT_FOUND")):
                raise RuntimeError(f"Erreur fatale API Gemini TTS (arrêt immédiat) : {err_str}") from err

            if essai == max_essais_reseau:
                raise RuntimeError(f"Échec après {max_essais_reseau} tentatives réseau : {err_str}") from err

            delai = essai * 3
            print(f"     [Retry réseau {essai}/{max_essais_reseau}] {err_str[:100]}. Attente {delai}s...", flush=True)
            time.sleep(delai)

    return False

def orchestrer_synthese_gemini_tts(script_vf, chemin_mp3_final, base_nom):
    map_locuteurs_voix = attribuer_voix_episode(script_vf)
    print(f"  -> Casting vocal attribué : {map_locuteurs_voix}", flush=True)

    dossier_tmp = tempfile.mkdtemp()
    fichiers_liste = []

    try:
        chemin_silence = os.path.join(dossier_tmp, "silence.mp3")
        cmd_silence = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "0.35", "-b:a", "128k",
            chemin_silence
        ]
        subprocess.run(cmd_silence, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        compteur_morceau = 0
        total_repliques = len(script_vf)

        for i, seg in enumerate(script_vf, start=1):
            if ARRET_DEMANDE:
                break

            texte_brut = seg["texte_fr"].strip()
            if not texte_brut:
                continue

            locuteur = seg["nom_locuteur"].strip()
            voix_voulue = map_locuteurs_voix[locuteur]

            print(f"  -> Synthèse réplique {i}/{total_repliques} ({locuteur} - Voix {voix_voulue})...", flush=True)
            texte_purifie = nettoyer_texte_pour_audio(texte_brut)
            sous_morceaux = decouper_texte_en_phrases(texte_purifie, max_chars=1800)

            for sous_bloc in sous_morceaux:
                if ARRET_DEMANDE:
                    break
                if not sous_bloc.strip():
                    continue
                clip_path = os.path.join(dossier_tmp, f"part_{compteur_morceau:05d}.mp3")
                succes = synthese_segment_gemini_tts(
                    segment=sous_bloc,
                    voix_name=voix_voulue,
                    chemin_mp3_sortie=clip_path,
                    dossier_tmp=dossier_tmp,
                    index_clip=compteur_morceau
                )
                if succes:
                    fichiers_liste.append(clip_path)
                    compteur_morceau += 1

            if not ARRET_DEMANDE:
                fichiers_liste.append(chemin_silence)

        if ARRET_DEMANDE:
            chemin_partiel = os.path.join("output", f"Podcast_VF_{base_nom}_PARTIEL.mp3")
            if assembler_fichiers_mp3(fichiers_liste, chemin_partiel, dossier_tmp):
                print(f"  💾 Audio partiel sauvegardé : Podcast_VF_{base_nom}_PARTIEL.mp3", flush=True)
            return

        if not assembler_fichiers_mp3(fichiers_liste, chemin_mp3_final, dossier_tmp):
            raise RuntimeError("Aucun segment audio n'a pu être assemblé.")

    finally:
        shutil.rmtree(dossier_tmp, ignore_errors=True)

# ---------------------------------------------------------
# ORCHESTRATION GLOBALE
# ---------------------------------------------------------
def executer_workflow():
    print(f"🎙️ Agent CastMorph démarré sur GitHub Actions (Moteur : {MODELE_TTS}).", flush=True)
    fichiers_audio = lister_audios_drive(DRIVE_FOLDER_ID)

    if not fichiers_audio:
        print("Aucun fichier audio trouvé dans le dossier Google Drive partagé.", flush=True)
        return

    print(f"📂 {len(fichiers_audio)} fichier(s) audio détecté(s) dans Drive.\n", flush=True)
    os.makedirs("output", exist_ok=True)
    dossier_telechargement = tempfile.mkdtemp()

    try:
        for f in fichiers_audio:
            if ARRET_DEMANDE:
                break

            nom_complet = f["name"]
            base_nom = nom_complet.rsplit(".", 1)[0]
            extension = os.path.splitext(nom_complet)[1]
            description_drive = f.get("description", "")

            chemin_vo = os.path.join("output", f"Transcription_VO_{base_nom}.txt")
            chemin_vf = os.path.join("output", f"Traduction_VF_{base_nom}.txt")
            chemin_mp3 = os.path.join("output", f"Podcast_VF_{base_nom}.mp3")

            if os.path.exists(chemin_vo) and os.path.exists(chemin_vf) and os.path.exists(chemin_mp3):
                print(f"⏭️ Déjà traité : {nom_complet}", flush=True)
                continue

            print("==================================================", flush=True)
            print(f"🎧 Traitement de : {nom_complet}", flush=True)
            print("==================================================", flush=True)

            chemin_local_source = os.path.join(dossier_telechargement, f"source_{f['id']}{extension}")

            try:
                telecharger_fichier_drive_robuste(f["id"], chemin_local_source)
                if ARRET_DEMANDE:
                    break
                print("  ✓ Fichier téléchargé depuis Google Drive.", flush=True)

                # Phase 1 : Transcription VO
                contexte = f"Nom du fichier : {base_nom}\nDescription : {description_drive}"
                script_vo = transcrire_et_diariser(chemin_local_source, contexte)
                if ARRET_DEMANDE or not script_vo:
                    break

                texte_export_vo = ""
                for rep in script_vo:
                    texte_export_vo += f"[{rep['nom_locuteur']} ({rep['genre']})]\n{rep['texte_original']}\n\n" + ("-" * 40) + "\n\n"

                with open(chemin_vo, "w", encoding="utf-8") as f_vo:
                    f_vo.write(texte_export_vo.strip())
                    f_vo.flush()
                print(f"  ✓ Phase 1 sauvegardée : Transcription_VO_{base_nom}.txt", flush=True)

                # Phase 2 : Traduction VF
                script_vf = traduire_dialogue(script_vo)
                if ARRET_DEMANDE or not script_vf:
                    break

                texte_export_vf = ""
                for rep in script_vf:
                    texte_export_vf += f"[{rep['nom_locuteur']}]\n{rep['texte_fr']}\n\n" + ("-" * 40) + "\n\n"

                with open(chemin_vf, "w", encoding="utf-8") as f_vf:
                    f_vf.write(texte_export_vf.strip())
                    f_vf.flush()
                print(f"  ✓ Phase 2 sauvegardée : Traduction_VF_{base_nom}.txt", flush=True)

                # Phase 3 : Synthèse Vocale Gemini 3.8 Flash Lite TTS
                orchestrer_synthese_gemini_tts(script_vf, chemin_mp3, base_nom)
                if not ARRET_DEMANDE and os.path.exists(chemin_mp3):
                    print(f"  ✓ Phase 3 sauvegardée : Podcast_VF_{base_nom}.mp3\n", flush=True)

            except RuntimeError as err_fatale:
                print(f"  ❌ {err_fatale}", flush=True)
                sys.exit(1)
            except Exception as err_fichier:
                print(f"  ❌ Erreur rencontrée sur « {nom_complet} » : {err_fichier}", flush=True)
                print("  ⏭️ Passage au fichier suivant...\n", flush=True)
            finally:
                if os.path.exists(chemin_local_source):
                    os.remove(chemin_local_source)

    finally:
        shutil.rmtree(dossier_telechargement, ignore_errors=True)
        print("🏁 Fin d'exécution. Empaquetage des résultats dans output/.", flush=True)

if __name__ == "__main__":
    executer_workflow()
