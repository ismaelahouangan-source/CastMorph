import os
import re
import sys
import json
import time
import signal
import random
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
EXTENSIONS_TEXTE = (".txt",)

MODELE_ANALYSE = "gemini-3.8-flash"

# ---------------------------------------------------------
# CASTING VOCAL : HÔTES VERROUILLÉS & INVITÉS ALÉATOIRES
# ---------------------------------------------------------
VOIX_VERROUILLEES = {
    "hannah fry": "fr-FR-DeniseNeural",
    "professor hannah fry": "fr-FR-DeniseNeural",
    "professeure hannah fry": "fr-FR-DeniseNeural",
    "professeur hannah fry": "fr-FR-DeniseNeural",
}

VOIX_HOMMES = [
    "fr-FR-HenriNeural",                 # Homme 1 (Grave, narratif)
    "fr-FR-RemyMultilingualNeural",      # Homme 2 (Médium, naturel)
    "fr-CA-AntoineNeural",               # Homme 3 (Dynamique)
    "fr-BE-GerardNeural",                # Homme 4 (Posé)
]

VOIX_FEMMES = [
    "fr-FR-DeniseNeural",                # Femme 1 (Douce, claire)
    "fr-FR-EloiseNeural",                # Femme 2 (Dynamique, vive)
    "fr-FR-VivienneMultilingualNeural",  # Femme 3 (Conversationnelle)
    "fr-BE-CharlineNeural",              # Femme 4 (Chaleureuse)
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
# FONCTIONS GOOGLE DRIVE (CONNEXION FRAÎCHE & RETRY)
# ---------------------------------------------------------
def get_drive_service():
    info_compte = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(info_compte, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def lister_fichiers_drive(folder_id):
    """Liste les fichiers TXT (traductions prêtes) et Audio présents dans Drive."""
    service = get_drive_service()
    requete = f"'{folder_id}' in parents and trashed = false"
    resultats = service.files().list(
        q=requete,
        pageSize=100,
        fields="files(id, name, description)"
    ).execute()
    fichiers = resultats.get("files", [])

    textes = [f for f in fichiers if f["name"].lower().endswith(EXTENSIONS_TEXTE)]
    audios = [f for f in fichiers if f["name"].lower().endswith(EXTENSIONS_AUDIO)]

    return (
        sorted(textes, key=lambda x: x["name"]),
        sorted(audios, key=lambda x: x["name"])
    )

def telecharger_fichier_drive_robuste(file_id, chemin_destination, max_essais=4):
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
            delai = essai * 4
            print(f"     [Retry Drive {essai}/{max_essais}] Coupure réseau ({err}). Reconnexion dans {delai}s...", flush=True)
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

def decouper_texte_en_blocs(texte: str, max_chars: int = 1500) -> list:
    """Découpe en blocs courts (~1 500 caractères) pour éviter les coupures WebSocket Edge-TTS."""
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
# LECTURE DIRECTE D'UN FICHIER TXT DÉJÀ TRADUIT
# ---------------------------------------------------------
def deduire_genres_locuteurs(noms_locuteurs: list) -> dict:
    if not noms_locuteurs:
        return {}

    prompt = f"""
    Détermine le genre ('masculin' ou 'feminin') de chacun des intervenants de podcast suivants d'après leur prénom/nom :
    {json.dumps(noms_locuteurs, ensure_ascii=False)}

    Réponds UNIQUEMENT par un objet JSON valide associant chaque nom exact à "masculin" ou "feminin".
    Exemple : {{"Hannah Fry": "feminin", "Maria Eckstein": "feminin", "Kim Stachenfeld": "feminin"}}
    """
    try:
        reponse = client_gemini.models.generate_content(
            model=MODELE_ANALYSE,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json"
            )
        )
        return json.loads(reponse.text)
    except Exception:
        return {nom: "masculin" for nom in noms_locuteurs}

def parser_fichier_traduction_txt(chemin_txt: str) -> list:
    with open(chemin_txt, "r", encoding="utf-8") as f:
        contenu = f.read()

    pattern = re.compile(r'^\[([^\]]+)\]\s*\n(.*?)(?=^\[[^\]]+\]\s*\n|\Z)', re.MULTILINE | re.DOTALL)
    matches = pattern.findall(contenu)

    repliques_brutes = []
    noms_sans_genre = []

    for entete, corps in matches:
        texte_propre = re.sub(r'\n*-{10,}\n*', '\n\n', corps).strip()
        if not texte_propre:
            continue

        match_genre = re.match(r'^(.*?)\s*\((masculin|feminin|féminin)\)\s*$', entete.strip(), re.IGNORECASE)
        if match_genre:
            nom = match_genre.group(1).strip()
            g_raw = match_genre.group(2).lower()
            genre = "feminin" if "fem" in g_raw else "masculin"
        else:
            nom = entete.strip()
            genre = None
            if nom not in noms_sans_genre:
                noms_sans_genre.append(nom)

        repliques_brutes.append({
            "nom_locuteur": nom,
            "genre": genre,
            "texte_fr": aerer_et_structurer_texte(texte_propre)
        })

    if not repliques_brutes:
        raise ValueError("Aucune réplique au format [Locuteur] n'a été détectée dans ce fichier TXT.")

    genres_deduits = deduire_genres_locuteurs(noms_sans_genre) if noms_sans_genre else {}

    for rep in repliques_brutes:
        if rep["genre"] is None:
            rep["genre"] = genres_deduits.get(rep["nom_locuteur"], "masculin")

    return repliques_brutes

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

    # 1. Verrouillage des hôtes récurrents (ex: Hannah Fry -> fr-FR-DeniseNeural)
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
# ÉTAPE 3 : SYNTHÈSE VOCALE EDGE-TTS SEGMENTÉE & RÉSILIENTE
# ---------------------------------------------------------
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

async def synthese_segment_edge_robuste(
    segment: str,
    voix_principale: str,
    voix_secours: str,
    chemin_fichier: str,
    max_essais: int = 5
) -> bool:
    for essai in range(1, max_essais + 1):
        if ARRET_DEMANDE:
            return False
        voix_active = voix_principale if essai <= 3 else voix_secours
        try:
            comm = edge_tts.Communicate(text=segment, voice=voix_active, rate="+0%", volume="+0%")
            await comm.save(chemin_fichier)
            if os.path.exists(chemin_fichier) and os.path.getsize(chemin_fichier) > 0:
                return True
        except Exception as err:
            delai = essai * 3
            print(f"     [Retry Edge-TTS {essai}/{max_essais} - {voix_active}] {err}. Nouvelle tentative dans {delai}s...", flush=True)
            fin_attente = time.time() + delai
            while time.time() < fin_attente and not ARRET_DEMANDE:
                await asyncio.sleep(min(0.5, fin_attente - time.time()))
    return False

async def orchestrer_synthese_edge_async(script_vf, chemin_mp3_final, base_nom):
    map_locuteurs_voix = attribuer_voix_episode(script_vf)
    print(f"  -> Casting vocal Edge-TTS attribué : {map_locuteurs_voix}", flush=True)

    dossier_tmp = tempfile.mkdtemp()
    fichiers_liste = []

    try:
        chemin_silence = os.path.join(dossier_tmp, "silence.mp3")
        cmd_silence = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "0.35", "-q:a", "9",
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
            genre = seg.get("genre", "masculin").lower()
            voix_voulue = map_locuteurs_voix[locuteur]
            voix_secours = "fr-FR-HenriNeural" if "fem" not in genre else "fr-FR-DeniseNeural"

            print(f"  -> Synthèse réplique {i}/{total_repliques} ({locuteur} - {voix_voulue})...", flush=True)
            texte_purifie = nettoyer_texte_pour_audio(texte_brut)
            sous_blocs = decouper_texte_en_blocs(texte_purifie, max_chars=1500)

            for sous_bloc in sous_blocs:
                if ARRET_DEMANDE:
                    break
                if not sous_bloc.strip():
                    continue
                clip_path = os.path.join(dossier_tmp, f"part_{compteur_morceau:05d}.mp3")
                succes = await synthese_segment_edge_robuste(
                    segment=sous_bloc,
                    voix_principale=voix_voulue,
                    voix_secours=voix_secours,
                    chemin_fichier=clip_path
                )
                if succes:
                    fichiers_liste.append(clip_path)
                    compteur_morceau += 1
                else:
                    print(f"     ⚠️ Échec définitif sur un sous-bloc de {locuteur}.", flush=True)

                await asyncio.sleep(0.8)

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
# ORCHESTRATION GLOBALE (SUPPORT AUDIO COMPLET + TXT DIRECT)
# ---------------------------------------------------------
def nettoyer_base_nom_txt(nom_fichier: str) -> str:
    base = nom_fichier.rsplit(".", 1)[0]
    for prefixe in ("Traduction_VF_", "traduction_vf_", "Traduction_", "VF_"):
        if base.startswith(prefixe):
            return base[len(prefixe):]
    return base

def executer_workflow():
    print("🎙️ Agent CastMorph démarré sur GitHub Actions (Moteur : Edge-TTS HD).", flush=True)
    fichiers_txt, fichiers_audio = lister_fichiers_drive(DRIVE_FOLDER_ID)

    if not fichiers_txt and not fichiers_audio:
        print("Aucun fichier audio ou texte (.txt) trouvé dans le dossier Google Drive partagé.", flush=True)
        return

    print(f"📂 Détecté dans Drive : {len(fichiers_txt)} fichier(s) TXT prêt(s) | {len(fichiers_audio)} fichier(s) Audio.\n", flush=True)
    os.makedirs("output", exist_ok=True)
    dossier_telechargement = tempfile.mkdtemp()
    bases_deja_traitees = set()

    try:
        # 1. Fichiers TXT déjà traduits -> Synthèse vocale directe (Phases 1 & 2 ignorées)
        for f_txt in fichiers_txt:
            if ARRET_DEMANDE:
                break

            nom_complet = f_txt["name"]
            base_nom = nettoyer_base_nom_txt(nom_complet)
            bases_deja_traitees.add(base_nom)

            chemin_vf_copie = os.path.join("output", f"Traduction_VF_{base_nom}.txt")
            chemin_mp3 = os.path.join("output", f"Podcast_VF_{base_nom}.mp3")

            if os.path.exists(chemin_mp3):
                print(f"⏭️ Déjà traité : {nom_complet}", flush=True)
                continue

            print("==================================================", flush=True)
            print(f"📄 Mode Direct TTS (Traduction fournie) : {nom_complet}", flush=True)
            print("==================================================", flush=True)

            chemin_local_txt = os.path.join(dossier_telechargement, f"source_{f_txt['id']}.txt")

            try:
                telecharger_fichier_drive_robuste(f_txt["id"], chemin_local_txt)
                if ARRET_DEMANDE:
                    break
                print("  ✓ Fichier TXT téléchargé depuis Google Drive.", flush=True)

                script_vf = parser_fichier_traduction_txt(chemin_local_txt)
                print(f"  ✓ {len(script_vf)} répliques chargées (Phases 1 et 2 ignorées).", flush=True)

                shutil.copyfile(chemin_local_txt, chemin_vf_copie)

                asyncio.run(orchestrer_synthese_edge_async(script_vf, chemin_mp3, base_nom))
                if not ARRET_DEMANDE and os.path.exists(chemin_mp3):
                    print(f"  ✓ Phase 3 sauvegardée : Podcast_VF_{base_nom}.mp3\n", flush=True)

            except Exception as err_fichier:
                print(f"  ❌ Erreur sur « {nom_complet} » : {err_fichier}", flush=True)
                print("  ⏭️ Passage au fichier suivant...\n", flush=True)
            finally:
                if os.path.exists(chemin_local_txt):
                    os.remove(chemin_local_txt)

        # 2. Fichiers audio -> Transcription -> Traduction -> Synthèse vocale
        for f in fichiers_audio:
            if ARRET_DEMANDE:
                break

            nom_complet = f["name"]
            base_nom = nom_complet.rsplit(".", 1)[0]

            if base_nom in bases_deja_traitees:
                print(f"⏭️ Audio « {nom_complet} » ignoré car déjà traité via son fichier TXT.", flush=True)
                continue

            extension = os.path.splitext(nom_complet)[1]
            description_drive = f.get("description", "")

            chemin_vo = os.path.join("output", f"Transcription_VO_{base_nom}.txt")
            chemin_vf = os.path.join("output", f"Traduction_VF_{base_nom}.txt")
            chemin_mp3 = os.path.join("output", f"Podcast_VF_{base_nom}.mp3")

            if os.path.exists(chemin_vo) and os.path.exists(chemin_vf) and os.path.exists(chemin_mp3):
                print(f"⏭️ Déjà traité : {nom_complet}", flush=True)
                continue

            print("==================================================", flush=True)
            print(f"🎧 Traitement complet de : {nom_complet}", flush=True)
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

                # Phase 3 : Synthèse Vocale Edge-TTS
                asyncio.run(orchestrer_synthese_edge_async(script_vf, chemin_mp3, base_nom))
                if not ARRET_DEMANDE and os.path.exists(chemin_mp3):
                    print(f"  ✓ Phase 3 sauvegardée : Podcast_VF_{base_nom}.mp3\n", flush=True)

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
