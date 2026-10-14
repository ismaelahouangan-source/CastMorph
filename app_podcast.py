import streamlit as st
import os
import json
import asyncio
import tempfile
import subprocess
import edge_tts
from google import genai
from google.genai import types

# ---------------------------------------------------------
# CONFIGURATION DE LA PAGE & VOIX HD
# ---------------------------------------------------------
st.set_page_config(
    page_title="CastMorph - Doublage de Podcasts HD",
    page_icon="🎙️",
    layout="wide"
)

# Réservoirs de voix pour différencier les locuteurs
VOIX_HOMMES = [
    "fr-FR-HenriNeural",             # Homme 1 (Grave, narratif)
    "fr-FR-RemyNeural",              # Homme 2 (Médium, naturel)
    "fr-CA-AntoineNeural",           # Homme 3 (Dynamique)
    "fr-BE-GerardNeural"             # Homme 4 (Posé)
]

VOIX_FEMMES = [
    "fr-FR-DeniseNeural",            # Femme 1 (Douce, naturelle)
    "fr-FR-EloiseNeural",            # Femme 2 (Claire, vive)
    "fr-FR-VivienneMultilingualNeural", # Femme 3 (Conversationnelle)
    "fr-BE-CharlineNeural"           # Femme 4 (Chaleureuse)
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
# ÉTAPE 2 : TRANSCRIPTION & DIARISATION (VO)
# ---------------------------------------------------------
def transcrire_et_diariser(chemin_audio: str, api_key: str, contexte: str):
    client = genai.Client(api_key=api_key)

    with st.spinner("Téléversement et analyse audio par Gemini..."):
        audio_file = client.files.upload(file=chemin_audio)

    prompt = f"""
    Tu es un ingénieur du son et transcripteur professionnel.
    Voici le contexte du podcast (titre, invités potentiels, sujet) :
    \"\"\"{contexte}\"\"\"

    CONSIGNES :
    1. Transcris fidèlement la parole en découpant par tour de rôle (diarisation).
    2. Identifie qui parle en t'aidant de la description fournie (ex: "Lex Fridman", "Sam Altman", ou "Animateur").
    3. Détermine le genre de chaque locuteur : strictement "masculin" ou "feminin".
    4. Rédige les propos originaux dans 'texte_original'.

    FORMAT DE SORTIE REQUIS (JSON pur uniquement) :
    [
      {{
        "speaker_id": "Speaker 1",
        "nom_locuteur": "Nom de la personne",
        "genre": "masculin",
        "texte_original": "Paroles prononcées en anglais..."
      }}
    ]
    """

    with st.spinner("Transcription et identification des intervenants en cours..."):
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
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

    return json.loads(reponse.text)

# ---------------------------------------------------------
# ÉTAPE 3 : TRADUCTION STRUCTURÉE (VF)
# ---------------------------------------------------------
def traduire_dialogue(script_vo, api_key: str):
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Tu es un adaptateur et traducteur professionnel pour le doublage audiovisuel.
    Traduis chaque réplique du dialogue suivant de l'anglais vers un français parlé fluide, naturel et élégant.

    CONSIGNES STRICTES :
    - Conserve exactement les mêmes locuteurs et la même structure JSON.
    - Le champ 'texte_fr' doit contenir UNIQUEMENT ce que la personne dit. Ne mets JAMAIS le nom de la personne, ni "Monsieur X :", ni astérisques Markdown (*) dans ce champ.
    
    DIALOGUE À TRADUIRE :
    {json.dumps(script_vo, ensure_ascii=False)}

    FORMAT DE SORTIE ATTENDU (JSON pur uniquement) :
    [
      {{
        "speaker_id": "Speaker 1",
        "nom_locuteur": "Nom de la personne",
        "genre": "masculin",
        "texte_original": "...",
        "texte_fr": "Traduction orale fluide en français..."
      }}
    ]
    """

    with st.spinner("Traduction du dialogue en français en cours..."):
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json"
            )
        )

    return json.loads(reponse.text)

# ---------------------------------------------------------
# ÉTAPE 4 : SYNTHÈSE VOCALE MULTI-VOIX VIA FFMPEG
# ---------------------------------------------------------
async def generer_clip_audio(texte: str, voix: str, chemin_fichier: str):
    comm = edge_tts.Communicate(texte, voix)
    await comm.save(chemin_fichier)

def orchestrer_synthese_vocale(script_vf):
    map_locuteurs_voix = {}
    index_h = 0
    index_f = 0

    # 1. Attribution des voix selon le genre
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

    total = len(script_vf)
    barre = st.progress(0, text="Initialisation de la synthèse vocale...")

    with tempfile.TemporaryDirectory() as dossier_tmp:
        # Fichier de silence (0.35s) généré avec ffmpeg
        chemin_silence = os.path.join(dossier_tmp, "silence.mp3")
        cmd_silence = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "0.35", "-q:a", "9",
            chemin_silence
        ]
        subprocess.run(cmd_silence, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        fichiers_liste = []

        # Génération segment par segment
        for i, seg in enumerate(script_vf):
            texte_a_lire = seg["texte_fr"].strip()
            if not texte_a_lire:
                continue

            voix = map_locuteurs_voix[seg["nom_locuteur"]]
            clip_path = os.path.join(dossier_tmp, f"replique_{i:04d}.mp3")

            asyncio.run(generer_clip_audio(texte_a_lire, voix, clip_path))

            # Ajout de la réplique et du micro-silence
            fichiers_liste.append(clip_path)
            fichiers_liste.append(chemin_silence)

            pct = int(((i + 1) / total) * 100)
            barre.progress(pct, text=f"Voix de {seg['nom_locuteur']} ({i + 1}/{total})...")

        # Fichier concat list pour ffmpeg
        concat_txt = os.path.join(dossier_tmp, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for item in fichiers_liste:
                f.write(f"file '{item}'\n")

        # Fusion sans ré-encodage via ffmpeg
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
    st.markdown("Importez un enregistrement audio pour générer une version française intégrale avec attribution de voix différenciées par locuteur.")
    st.divider()

    # Barre latérale : Clé API & Réinitialisation
    with st.sidebar:
        st.header("⚙️ Paramètres")
        cle_automatique = st.secrets.get("GOOGLE_API_KEY", "")
        api_key = st.text_input("Clé API Google Gemini :", value=cle_automatique, type="password")
        
        if st.button("🔄 Nouveau Podcast / Réinitialiser", use_container_width=True):
            reinitialiser_tout()
            st.rerun()

    # -----------------------------------------------------
    # ÉTAPE 1 : IMPORTATION DU FICHIER AUDIO
    # -----------------------------------------------------
    st.subheader("Étape 1 : Importer la Piste Audio")
    
    fichier_upload = st.file_uploader(
        "Sélectionnez le fichier audio du podcast (Anglais) :",
        type=["mp3", "wav", "m4a", "aac", "ogg"]
    )

    contexte_manuel = st.text_area(
        "Contexte de l'émission (Facultatif - Titre, nom des invités pour aider l'IA) :",
        placeholder="Ex: Épisode avec Sam Altman et Lex Fridman parlant d'intelligence artificielle...",
        height=100
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

        # Affichage et Téléchargement de la VO
        if st.session_state.script_vo:
            with st.expander("👁️ Voir la transcription originale (Anglais)", expanded=False):
                texte_vo = ""
                for rep in st.session_state.script_vo:
                    ligne = f"[{rep['nom_locuteur']} ({rep['genre']})] : {rep['texte_original']}\n\n"
                    texte_vo += ligne
                    st.markdown(f"**{rep['nom_locuteur']}** ({rep['genre']}) : {rep['texte_original']}")

            st.download_button(
                label="📄 Télécharger la Transcription originale (.txt)",
                data=texte_vo,
                file_name="transcription_originale_VO.txt",
                mime="text/plain"
            )

    # -----------------------------------------------------
    # ÉTAPE 3 : TRADUCTION FRANÇAISE (VF)
    # -----------------------------------------------------
    if st.session_state.script_vo:
        st.divider()
        st.subheader("Étape 3 : Traduction en Français")

        if st.button("🌐 Lancer la Traduction en Français", type="primary"):
            if not api_key.strip():
                st.error("Clé API requise.")
            else:
                try:
                    vf = traduire_dialogue(st.session_state.script_vo, api_key)
                    st.session_state.script_vf = vf
                    st.success("Traduction du dialogue terminée !")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur de traduction : {str(e)}")

        # Affichage et Téléchargement de la VF
        if st.session_state.script_vf:
            with st.expander("👁️ Voir le dialogue traduit (Français)", expanded=True):
                texte_vf = ""
                for rep in st.session_state.script_vf:
                    badge = "👨" if rep.get("genre") == "masculin" else "👩"
                    ligne = f"[{rep['nom_locuteur']}] : {rep['texte_fr']}\n\n"
                    texte_vf += ligne
                    st.markdown(f"**{badge} {rep['nom_locuteur']}** : {rep['texte_fr']}")

            st.download_button(
                label="📄 Télécharger le Script Traduit (.txt)",
                data=texte_vf,
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
                st.success("Doublage vocal achevé !")
                st.rerun()
            except Exception as e:
                st.error(f"Erreur audio : {str(e)}")

        # Lecteur et Téléchargement Audio MP3
        if st.session_state.audio_final_bytes:
            st.write("---")
            st.subheader("🔊 Résultat Final : Podcast Doublé")
            
            # Affichage de la distribution des voix
            cols = st.columns(len(st.session_state.map_voix))
            for i, (nom, voix) in enumerate(st.session_state.map_voix.items()):
                nom_court = voix.split("-")[-1].replace("Neural", "")
                cols[i].metric(label=nom, value=f"Voix {nom_court}")

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
