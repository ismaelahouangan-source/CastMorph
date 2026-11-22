import os
import re
import json
import time
import shutil
import asyncio
import tempfile
import subprocess
import edge_tts
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

VOIX_HOMMES = [
    "fr-FR-HenriNeural",                 # Homme 1 (Grave, narratif)
    "fr-FR-RemyMultilingualNeural",      # Homme 2 (Médium, naturel)
    "fr-CA-AntoineNeural",               # Homme 3 (Dynamique)
    "fr-BE-GerardNeural"                 # Homme 4 (Posé)
]

VOIX_FEMMES = [
    "fr-FR-DeniseNeural",                # Femme 1 (Douce, claire)
    "fr-FR-EloiseNeural",                # Femme 2 (Dynamique, vive)
    "fr-FR-VivienneMultilingualNeural",  # Femme 3 (Conversationnelle)
    "fr-BE-CharlineNeural"               # Femme 4 (Chaleureuse)
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
    """Télécharge le fichier avec réinitialisation de la connexion SSL en cas de Broken Pipe."""
    for essai in range(1, max_essais + 1):
        try:
            # On recrée le client Drive à chaque tentative pour éviter les sockets expirés
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

def decouper_texte_en_phrases(texte: str, max_chars: int = 1400) -> list:
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
# ÉTAPE 1 : TRANSCRIPTION & DIARISATION (AVEC ATTENTE ACTIVE)
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
    2. Identifie qui parle en t'aidant du contexte (ex: "Donald Miller", "Voix off", "Invité", "Animateur").
    3. Détermine le genre de chaque locuteur : strictement "masculin" ou "feminin".
    4. STRUCTURE ET AÉRATION : Si une prise de parole est longue, aère-la impérativement en plusieurs paragraphes distincts séparés par un double saut de ligne (\\n\\n). N'aplatis JAMAIS une longue intervention en un bloc compact continu.
    5. N'utilise AUCUN balisage Markdown (pas d'astérisques *, pas de dièses #).

    FORMAT DE SORTIE REQUIS (JSON pur uniquement) :
    [
      {{
        "speaker_id": "Speaker 1",
        "nom_locuteur": "Nom de la personne",
        "genre": "masculin",
        "texte_original": "Paroles prononcées en anglais aérées avec des \\n\\n..."
      }}
    ]
    """

    print("  -> Transcription et diarisation en cours (Gemini 3.8 Flash)...")
    try:
        reponse = client_gemini.models.generate_content(
            model="gemini-3.8-flash",
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
        "nom_locuteur": "Nom de la personne",
        "genre": "masculin",
        "texte_original": "...",
        "texte_fr": "Premier paragraphe traduit.\\n\\nDeuxième paragraphe bien aéré..."
      }}
    ]
    """

    reponse = client_gemini.models.generate_content(
        model="gemini-3.8-flash",
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
# ÉTAPE 3 : SYNTHÈSE VOCALE ROBUSTE MULTI-VOIX
# ---------------------------------------------------------
async def synthese_segment_robuste(segment: str, voix_principale: str, voix_secours: str, chemin_fichier: str, max_essais: int = 5):
    for essai in range(1, max_essais + 1):
        voix_active = voix_principale if essai <= 3 else voix_secours
        try:
            comm = edge_tts.Communicate(text=segment, voice=voix_active, rate="+0%", volume="+0%")
            await comm.save(chemin_fichier)
            if os.path.exists(chemin_fichier) and os.path.getsize(chemin_fichier) > 0:
                return True
        except Exception as err:
            delai = essai * 3
            print(f"     [Retry TTS {essai}/{max_essais}] Erreur ({err}). Attente {delai}s...")
            await asyncio.sleep(delai)
    return False

async def orchestrer_synthese_async(script_vf, chemin_mp3_final):
    map_locuteurs_voix = {}
    index_h = 0
    index_f = 0

    for seg in script_vf:
        locuteur = seg["nom_locuteur"]
        genre = seg.get("genre", "masculin").lower()

        if locuteur not in map_locuteurs_voix:
            if genre == "feminin":
                map_locuteurs_voix[locuteur] = VOIX_FEMMES[index_f % len(VOIX_FEMMES)]
                index_f += 1
            else:
                map_locuteurs_voix[locuteur] = VOIX_HOMMES[index_h % len(VOIX_HOMMES)]
                index_h += 1

    print(f"  -> Casting vocal attribué : {map_locuteurs_voix}")

    dossier_tmp = tempfile.mkdtemp()
    try:
        chemin_silence = os.path.join(dossier_tmp, "silence.mp3")
        cmd_silence = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "0.35", "-q:a", "9",
            chemin_silence
        ]
        subprocess.run(cmd_silence, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        fichiers_liste = []
        compteur_morceau = 0
        total_repliques = len(script_vf)

        for i, seg in enumerate(script_vf, start=1):
            texte_brut = seg["texte_fr"].strip()
            if not texte_brut:
                continue

            locuteur = seg["nom_locuteur"]
            genre = seg.get("genre", "masculin").lower()
            voix_voulue = map_locuteurs_voix[locuteur]
            voix_secours = "fr-FR-HenriNeural" if genre != "feminin" else "fr-FR-DeniseNeural"

            print(f"  -> Synthèse réplique {i}/{total_repliques} ({locuteur} - {voix_voulue})...")
            texte_purifie = nettoyer_texte_pour_audio(texte_brut)
            sous_morceaux = decouper_texte_en_phrases(texte_purifie, max_chars=1400)

            for sous_bloc in sous_morceaux:
                if not sous_bloc.strip():
                    continue
                clip_path = os.path.join(dossier_tmp, f"part_{compteur_morceau:05d}.mp3")
                succes = await synthese_segment_robuste(sous_bloc, voix_voulue, voix_secours, clip_path)
                if succes:
                    fichiers_liste.append(clip_path)
                    compteur_morceau += 1
                else:
                    print(f"     ⚠️ Échec définitif sur un sous-bloc de {locuteur}.")
                await asyncio.sleep(1.0)

            fichiers_liste.append(chemin_silence)

        concat_txt = os.path.join(dossier_tmp, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for item in fichiers_liste:
                f.write(f"file '{item}'\n")

        cmd_concat = [
            "ffmpeg", "-y", "-f", "concat",
            "-safe", "0", "-i", concat_txt,
            "-c", "copy", chemin_mp3_final
        ]
        subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    finally:
        shutil.rmtree(dossier_tmp, ignore_errors=True)

# ---------------------------------------------------------
# ORCHESTRATION GLOBALE RÉSILIENTE
# ---------------------------------------------------------
def executer_workflow():
    print("🎙️ Agent CastMorph démarré sur GitHub Actions.")
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

            print(f"==================================================")
            print(f"🎧 Traitement de : {nom_complet}")
            print(f"==================================================")

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

                asyncio.run(orchestrer_synthese_async(script_vf, chemin_mp3))
                print(f"  ✓ Podcast doublé généré : Podcast_VF_{base_nom}.mp3\n")

            except Exception as err_fichier:
                print(f"  ❌ Erreur rencontrée sur « {nom_complet} » : {err_fichier}")
                print("  ⏭️ Passage au fichier suivant...\n")
            finally:
                # Nettoyage immédiat de la source locale pour préserver l'espace disque
                if os.path.exists(chemin_local_source):
                    os.remove(chemin_local_source)

            time.sleep(3)

    finally:
        shutil.rmtree(dossier_telechargement, ignore_errors=True)
        print("🏁 Traitement de la file terminé.")

if __name__ == "__main__":
    executer_workflow()
