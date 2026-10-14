import streamlit as st
import os
import json
import asyncio
import tempfile
import subprocess
import re
import edge_tts
from google import genai
from google.genai import types

# ---------------------------------------------------------
# CONFIGURATION DE LA PAGE & VOIX VALIDÉES EDGE-TTS
# ---------------------------------------------------------
st.set_page_config(
    page_title="CastMorph - Studio de Doublage HD",
    page_icon="🎙️",
    layout="wide"
)

# Voix neuronales françaises certifiées
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

# ---------------------------------------------------------
# INITIALISATION DE LA MÉMOIRE (SESSION STATE)
# ---------------------------------------------------------
if "chemin_audio" not in st.session_state:
    st.session_state.chemin_audio = None
if "contexte_info" not in st.session_state:
    st.session_state.contexte_info = ""
if "script_vo" not in st.session_state:
    st.session_state.script_vo = None
if "script_vf" not in st.session_state:
    st.session_state.script_vf = None
if "audio_final_bytes" not in st.session_state:
    st.session_state.audio_final_bytes = None
if "map_voix" not in st.session_state:
    st.session_state.map_voix = {}

def reinitialiser_tout():
    st.session_state.chemin_audio = None
    st.session_state.contexte_info = ""
    st.session_state.script_vo = None
    st.session_state.script_vf = None
    st.session_state.audio_final_bytes = None
    st.session_state.map_voix = {}

# ---------------------------------------------------------
# FONCTIONS DE NETTOYAGE & D'AÉRATION TYPOGRAPHIQUE
# ---------------------------------------------------------
def aérer_et_structurer_texte(texte: str) -> str:
    """Harmonise les retours à la ligne pour offrir des paragraphes aérés et lisibles."""
    if not texte:
        return ""
    # Suppression des astérisques Markdown et soulignés parasites
    texte = texte.replace("*", "").replace("_", "")
    # Remplacement des sauts de ligne excessifs par un double saut standard
    texte = re.sub(r'\r\n', '\n', texte)
    texte = re.sub(r'\n{3,}', '\n\n', texte)
    # Suppression des espaces horizontaux multiples
    texte = re.sub(r'[ \t]+', ' ', texte)
    return texte.strip()

def nettoyer_texte_pour_audio(texte: str) -> str:
    """Élimine tous les artefacts nuisibles à la prononciation du moteur Edge-TTS."""
    if not texte:
        return ""
    # 1. Correction du bug de lecture horaire (ex: 38:4 -> 38, 4)
    texte = re.sub(r'(\d+):(\d+)', r'\1, \2', texte)
    # 2. Suppression totale des balises et symboles Markdown
    texte = texte.replace("*", "").replace("#", "").replace("_", "")
    # 3. Normalisation des tirets cadratins en virgules pour une pause orale naturelle
    texte = re.sub(r'\s*[-—–]\s*', ', ', texte)
    # 4. Harmonisation des guillemets
    texte = texte.replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
    # 5. Espaces et ponctuation
    texte = re.sub(r'\s+([.,!?:;])', r'\1', texte)
    texte = re.sub(r'[ \t]+', ' ', texte)
    return texte.strip()

def decouper_texte_en_phrases(texte: str, max_chars: int = 1400) -> list:
    """Découpe les longues répliques en sous-blocs digestes pour ne jamais saturer Edge-TTS."""
    # On commence par respecter les paragraphes s'ils existent
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
# ÉTAPE 2 : TRANSCRIPTION & DIARISATION (VO)
# ---------------------------------------------------------
def transcrire_et_diariser(chemin_audio: str, api_key: str, contexte: str):
    client = genai.Client(api_key=api_key)

    with st.spinner("Téléversement et analyse audio par Gemini..."):
        audio_file = client.files.upload(file=chemin_audio)

    prompt = f"""
    Tu es un ingénieur du son et transcripteur professionnel.
    Voici le contexte de l'émission :
    \"\"\"{contexte}\"\"\"

    CONSIGNES CRITIQUES :
    1. Transcris fidèlement la parole en découpant par tour de rôle (diarisation).
    2. Identifie qui parle en t'aidant de la description fournie (ex: "Donald Miller", "Voix off", "Invité").
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

    with st.spinner("Transcription et identification des intervenants en cours..."):
        reponse = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=[audio_file, prompt],
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json"
            )
        )

    try:
        client.files.delete(name=audio_file.name)
    except Exception:
        pass

    donnees = json.loads(reponse.text)
    for seg in donnees:
        seg["texte_original"] = aérer_et_structurer_texte(seg.get("texte_original", ""))
    return donnees

# ---------------------------------------------------------
# ÉTAPE 3 : TRADUCTION STRUCTURÉE & AÉRÉE (VF)
# ---------------------------------------------------------
def traduire_dialogue(script_vo, api_key: str):
    client = genai.Client(api_key=api_key)

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

    with st.spinner("Traduction et structuration du dialogue en cours..."):
        reponse = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json"
            )
        )

    donnees = json.loads(reponse.text)
    for seg in donnees:
        seg["texte_fr"] = aérer_et_structurer_texte(seg.get("texte_fr", ""))
    return donnees

# ---------------------------------------------------------
# ÉTAPE 4 : SYNTHÈSE VOCALE SÉCURISÉE MULTI-VOIX
# ---------------------------------------------------------
async def generer_clip_avec_secours(texte: str, voix_principale: str, voix_secours: str, chemin_sortie: str):
    try:
        comm = edge_tts.Communicate(texte, voix_principale)
        await comm.save(chemin_sortie)
    except Exception:
        comm = edge_tts.Communicate(texte, voix_secours)
        await comm.save(chemin_sortie)

def orchestrer_synthese_vocale(script_vf):
    map_locuteurs_voix = {}
    index_h = 0
    index_f = 0

    # Attribution dynamique des voix
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

    total_repliques = len(script_vf)
    barre = st.progress(0, text="Initialisation du studio audio...")

    with tempfile.TemporaryDirectory() as dossier_tmp:
        # Micro-pause de 0.35 s entre répliques
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

        for i, seg in enumerate(script_vf):
            texte_brut = seg["texte_fr"].strip()
            if not texte_brut:
                continue

            locuteur = seg["nom_locuteur"]
            genre = seg.get("genre", "masculin").lower()
            voix_voulue = map_locuteurs_voix[locuteur]
            voix_secours = "fr-FR-HenriNeural" if genre != "feminin" else "fr-FR-DeniseNeural"

            pct = int((i / total_repliques) * 100)
            barre.progress(pct, text=f"Enregistrement de {locuteur} ({i + 1}/{total_repliques})...")

            # Nettoyage phonétique préalable
            texte_purifie = nettoyer_texte_pour_audio(texte_brut)

            # Découpage préventif anti-surchauffe WebSocket
            sous_morceaux = decouper_texte_en_phrases(texte_purifie, max_chars=1400)

            for sous_bloc in sous_morceaux:
                if not sous_bloc.strip():
                    continue
                clip_path = os.path.join(dossier_tmp, f"part_{compteur_morceau:05d}.mp3")
                asyncio.run(generer_clip_avec_secours(sous_bloc, voix_voulue, voix_secours, clip_path))
                fichiers_liste.append(clip_path)
                compteur_morceau += 1

            # Pause naturelle avant le changement d'interlocuteur
            fichiers_liste.append(chemin_silence)

        barre.progress(100, text="Assemblage et finalisation du podcast complet...")

        # Concaténation ffmpeg
        concat_txt = os.path.join(dossier_tmp, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for item in fichiers_liste:
                f.write(f"file '{item}'\n")

        fichier_mp3_final = os.path.join(dossier_tmp, "podcast_vf_complet.mp3")
        cmd_concat = [
            "ffmpeg", "-y", "-f", "concat",
            "-safe", "0", "-i", concat_txt,
            "-c", "copy", fichier_mp3_final
        ]
        subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        with open(fichier_mp3_final, "rb") as f:
            audio_bytes = f.read()

    return audio_bytes, map_locuteurs_voix

# ---------------------------------------------------------
# INTERFACE PRINCIPALE STREAMLIT
# ---------------------------------------------------------
def main():
    st.title("🎙️ CastMorph - Doublage Automatique de Podcasts")
    st.markdown("Pipeline haute fidélité : Transcription Diarisée • Traduction Aérée • Synthèse Vocale Multi-Voix HD.")
    st.divider()

    with st.sidebar:
        st.header("⚙️ Paramètres")
        cle_automatique = st.secrets.get("GOOGLE_API_KEY", "")
        api_key = st.text_input("Clé API Google Gemini :", value=cle_automatique, type="password")
        
        if st.button("🔄 Nouveau Podcast / Réinitialiser", use_container_width=True):
            reinitialiser_tout()
            st.rerun()

    # -----------------------------------------------------
    # ÉTAPE 1 : IMPORTATION AUDIO
    # -----------------------------------------------------
    st.subheader("Étape 1 : Importer la Piste Audio")
    
    fichier_upload = st.file_uploader(
        "Sélectionnez le fichier audio du podcast (Anglais) :",
        type=["mp3", "wav", "m4a", "aac", "ogg"]
    )

    contexte_manuel = st.text_area(
        "Contexte de l'émission (Facultatif - Titre, invités pour guider l'IA) :",
        placeholder="Ex: Épisode avec Donald Miller sur les 5 formules marketing StoryBrand...",
        height=80
    )

    if fichier_upload is not None:
        if st.session_state.chemin_audio is None:
            extension = os.path.splitext(fichier_upload.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as temp_f:
                temp_f.write(fichier_upload.read())
                st.session_state.chemin_audio = temp_f.name
                st.session_state.contexte_info = f"Fichier : {fichier_upload.name}\n{contexte_manuel}"
            st.success(f"Fichier « {fichier_upload.name} » prêt pour l'analyse.")

    # -----------------------------------------------------
    # ÉTAPE 2 : TRANSCRIPTION & DIARISATION (VO)
    # -----------------------------------------------------
    if st.session_state.chemin_audio:
        st.divider()
        st.subheader("Étape 2 : Transcription & Identification des Intervenants (VO)")

        if st.button("📝 Lancer la Transcription & Diarisation", type="primary"):
            if not api_key.strip():
                st.error("Veuillez renseigner votre clé API Gemini dans le menu latéral.")
            else:
                try:
                    vo = transcrire_et_diariser(
                        st.session_state.chemin_audio,
                        api_key,
                        st.session_state.contexte_info
                    )
                    st.session_state.script_vo = vo
                    st.success(f"{len(vo)} prises de parole identifiées !")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur de transcription : {str(e)}")

        if st.session_state.script_vo:
            with st.expander("👁️ Voir la transcription originale aérée (Anglais)", expanded=False):
                texte_export_vo = ""
                for rep in st.session_state.script_vo:
                    st.markdown(f"### 🎙️ {rep['nom_locuteur']} ({rep['genre']})")
                    paragraphes = rep['texte_original'].split("\n\n")
                    for p in paragraphes:
                        st.write(p)
                    texte_export_vo += f"[{rep['nom_locuteur']} ({rep['genre']})]\n{rep['texte_original']}\n\n" + ("-" * 40) + "\n\n"

            st.download_button(
                label="📄 Télécharger la Transcription originale structurée (.txt)",
                data=texte_export_vo,
                file_name="transcription_originale_VO.txt",
                mime="text/plain"
            )

    # -----------------------------------------------------
    # ÉTAPE 3 : TRADUCTION STRUCTURÉE & AÉRÉE (VF)
    # -----------------------------------------------------
    if st.session_state.script_vo:
        st.divider()
        st.subheader("Étape 3 : Traduction en Français (Paragraphes Aérés)")

        if st.button("🌐 Lancer la Traduction en Français", type="primary"):
            if not api_key.strip():
                st.error("Clé API requise.")
            else:
                try:
                    vf = traduire_dialogue(st.session_state.script_vo, api_key)
                    st.session_state.script_vf = vf
                    st.success("Traduction du dialogue structurée avec succès !")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur de traduction : {str(e)}")

        if st.session_state.script_vf:
            with st.expander("👁️ Consulter le script traduit et aéré (Français)", expanded=True):
                texte_export_vf = ""
                for rep in st.session_state.script_vf:
                    badge = "👨" if rep.get("genre") == "masculin" else "👩"
                    st.markdown(f"### {badge} {rep['nom_locuteur']}")
                    paragraphes = rep['texte_fr'].split("\n\n")
                    for p in paragraphes:
                        st.write(p)
                    texte_export_vf += f"[{rep['nom_locuteur']}]\n{rep['texte_fr']}\n\n" + ("-" * 40) + "\n\n"

            st.download_button(
                label="📄 Télécharger le Script Traduit & Aéré (.txt)",
                data=texte_export_vf,
                file_name="script_traduit_VF.txt",
                mime="text/plain"
            )

    # -----------------------------------------------------
    # ÉTAPE 4 : SYNTHÈSE VOCALE MULTI-VOIX
    # -----------------------------------------------------
    if st.session_state.script_vf:
        st.divider()
        st.subheader("Étape 4 : Synthèse Vocale HD (Multi-Voix)")

        if st.button("🎧 Générer la Version Audio Française (Multi-Voix)", type="primary"):
            try:
                audio_bytes, map_voix = orchestrer_synthese_vocale(st.session_state.script_vf)
                st.session_state.audio_final_bytes = audio_bytes
                st.session_state.map_voix = map_voix
                st.success("Podcast doublé avec succès !")
                st.rerun()
            except Exception as e:
                st.error(f"Erreur audio : {str(e)}")

        if st.session_state.audio_final_bytes:
            st.write("---")
            st.subheader("🔊 Résultat Final : Podcast Doublé")
            
            # Affichage de la distribution des voix
            cols = st.columns(len(st.session_state.map_voix))
            for i, (nom, voix) in enumerate(st.session_state.map_voix.items()):
                nom_court = voix.split("-")[-1].replace("Neural", "").replace("Multilingual", " (Multi)")
                cols[i].metric(label=nom, value=nom_court)

            st.audio(st.session_state.audio_final_bytes, format="audio/mp3")

            st.download_button(
                label="⬇️ Télécharger le Podcast Doublé Complet (.mp3)",
                data=st.session_state.audio_final_bytes,
                file_name="podcast_vf_multi_voix.mp3",
                mime="audio/mp3",
                type="primary"
            )

if __name__ == "__main__":
    main()
