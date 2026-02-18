from __future__ import annotations

from datetime import date
from openai import OpenAI
import time
import os
from dotenv import load_dotenv
load_dotenv()


def call_gpt52(prompt: str,modele : str) -> str:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    today = date.today().isoformat()

    instructions = (
        "Tu es un assistant conversationnel généraliste.\n"
        "Règles obligatoires :\n"
        "Donner des valeurs numériques précises lorsque possible (éviter les arrondis grossiers).\n"
        "Privilégier la précision numérique plutôt que la lisibilité simplifiée.\n"
        "Éviter les arrondis grossiers (ex: 10 % au lieu de 11,3 %).\n"
        "Réponds de façon utile, naturelle, claire, avec des paragraphes lisibles.\n"
        f"Nous sommes le {today} (fuseau Europe/Paris)."
    )

    resp = client.responses.create(
        model=modele,
        instructions=instructions,
        input=prompt,
        temperature=0.8,
        top_p=1,
        tools=[{
            "type": "web_search",
            "user_location": {
                "type": "approximate",
                "country": "FR",
                "city": "Paris",
                "timezone": "Europe/Paris",
            },
        }],
        tool_choice="auto",
        include=["web_search_call.action.sources"],
    )

    # "raw" = le texte complet agrégé
    return resp.output_text or ""


if __name__ == "__main__":
    print("début")
    start = time.perf_counter()
    prompt = "Je veux vérifier une affirmation : « La croissance française est systématiquement inférieure à celle de la zone euro depuis 2010 »"
    print("prompt=" +prompt)
    modele="gpt-5.2"
    print("réponse de "+modele+"="+call_gpt52(prompt,modele))
    end = time.perf_counter()
    print(f"Durée totale d'exécution : {end - start:.2f} secondes")
