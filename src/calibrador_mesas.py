"""
Ferramenta Interativa de Calibração de Zonas (Mesas).
Abre um frame de referência e permite ao usuário clicar com o mouse
para desenhar os polígonos de cada mesa, salvando tudo em um arquivo JSON.

Instruções:
- Clique com o BOTÃO ESQUERDO para marcar vértices do polígono.
- Pressione 'f' para finalizar a mesa atual e digitar o nome dela no terminal.
- Pressione 'c' para cancelar a mesa que está sendo desenhada.
- Pressione 's' para salvar as mesas em 'data/mesas_config.json'.
- Pressione 'q' para sair.
"""

import sys
import json
from pathlib import Path
import cv2
import numpy as np

# Força UTF-8 no stdout para compatibilidade com terminais Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

mesas = []          # Lista de dicionários: [{"id": "Mesa 1", "pontos": [[x, y], ...]}, ...]
pontos_atuais = []  # Vértices da mesa que está sendo desenhada
frame_original = None
frame_display = None

def callback_mouse(event, x, y, flags, param):
    global pontos_atuais
    if event == cv2.EVENT_LBUTTONDOWN:
        pontos_atuais.append([x, y])
        print(f"  📍 Ponto adicionado: ({x}, {y}) [Total de pontos na mesa atual: {len(pontos_atuais)}]")

def desenhar_overlay():
    display = frame_original.copy()

    # Desenha mesas já salvas
    for mesa in mesas:
        pts = np.array(mesa["pontos"], np.int32).reshape((-1, 1, 2))
        cv2.polylines(display, [pts], isClosed=True, color=(0, 255, 0), thickness=2)
        
        # Preenchimento translúcido
        overlay = display.copy()
        cv2.fillPoly(overlay, [pts], color=(0, 255, 0))
        cv2.addWeighted(overlay, 0.2, display, 0.8, 0, display)

        # Rótulo com o nome da mesa
        p1 = mesa["pontos"][0]
        cv2.putText(display, mesa["id"], (p1[0], max(p1[1] - 8, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    # Desenha pontos da mesa em construção
    if len(pontos_atuais) > 0:
        for pt in pontos_atuais:
            cv2.circle(display, tuple(pt), 4, (0, 255, 255), -1)
        if len(pontos_atuais) > 1:
            pts_em_andamento = np.array(pontos_atuais, np.int32).reshape((-1, 1, 2))
            cv2.polylines(display, [pts_em_andamento], isClosed=False, color=(0, 255, 255), thickness=2)

    # Legenda superior
    cv2.rectangle(display, (0, 0), (display.shape[1], 36), (20, 20, 20), -1)
    texto_ajuda = "[Clique]: Adicionar ponto | [F]: Fechar mesa | [C]: Cancelar | [S]: Salvar | [Q]: Sair"
    cv2.putText(display, texto_ajuda, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    return display

def iniciar_calibracao(imagem_path: str = "data/videos/frame_referencia.jpg", json_output: str = "data/mesas_config.json"):
    global frame_original, frame_display, pontos_atuais, mesas

    img_path = Path(imagem_path)
    if not img_path.exists():
        print(f"❌ Imagem de referência '{imagem_path}' não encontrada. Rode 'test_video.py' primeiro!")
        return

    frame_original = cv2.imread(str(img_path))
    if frame_original is None:
        print(f"❌ Não foi possível carregar a imagem '{imagem_path}'.")
        return

    # Se já existir um arquivo json anterior, carrega para edição
    json_path = Path(json_output)
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                mesas = json.load(f).get("mesas", [])
            print(f"📂 Carregadas {len(mesas)} mesas já configuradas anteriormente.")
        except Exception as e:
            print(f"⚠️ Não foi possível ler JSON existente: {e}")

    nome_janela = "Calibrador de Mesas (ROI Editor)"
    cv2.namedWindow(nome_janela)
    cv2.setMouseCallback(nome_janela, callback_mouse)

    print("\n" + "=" * 60)
    print("🛠️ INICIANDO CALIBRADOR DE MESAS")
    print("  • Clique com o mouse para marcar os cantos da mesa.")
    print("  • Pressione 'f' quando terminar o contorno da mesa.")
    print("  • Pressione 's' para salvar as mesas no JSON.")
    print("  • Pressione 'q' para sair.")
    print("=" * 60 + "\n")

    while True:
        display = desenhar_overlay()
        cv2.imshow(nome_janela, display)

        tecla = cv2.waitKey(20) & 0xFF

        # 'f' -> Fechar e registrar mesa
        if tecla == ord('f') or tecla == ord('F'):
            if len(pontos_atuais) >= 3:
                num_mesa = len(mesas) + 1
                nome_mesa = input(f"👉 Digite o nome para esta mesa [Padrão: 'Mesa {num_mesa}']: ").strip()
                if not nome_mesa:
                    nome_mesa = f"Mesa {num_mesa}"
                
                mesas.append({"id": nome_mesa, "pontos": list(pontos_atuais)})
                print(f"✅ {nome_mesa} cadastrada com {len(pontos_atuais)} vértices!")
                pontos_atuais = []
            else:
                print("⚠️ São necessários pelo menos 3 pontos para formar um polígono.")

        # 'c' -> Cancelar desenho em andamento
        elif tecla == ord('c') or tecla == ord('C'):
            pontos_atuais = []
            print("🔄 Contorno em andamento cancelado.")

        # 's' -> Salvar no arquivo JSON
        elif tecla == ord('s') or tecla == ord('S'):
            json_path.parent.mkdir(parents=True, exist_ok=True)
            dados = {
                "imagem_referencia": str(img_path.name),
                "largura": frame_original.shape[1],
                "altura": frame_original.shape[0],
                "mesas": mesas
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(dados, f, indent=2, ensure_ascii=False)
            print(f"\n💾 Configuração salva com sucesso em: {json_path.resolve()} ({len(mesas)} mesas)")

        # 'q' ou ESC -> Sair
        elif tecla == ord('q') or tecla == ord('Q') or tecla == 27:
            print("👋 Encerrando calibrador.")
            break

    cv2.destroyAllWindows()

if __name__ == "__main__":
    iniciar_calibracao()
