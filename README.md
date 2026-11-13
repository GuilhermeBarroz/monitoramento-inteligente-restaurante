# 🍽️ Monitoramento de Câmeras com IA para Restaurantes

Sistema inteligente de Visão Computacional para monitoramento em tempo real de ocupação de mesas, controle de tempo de atendimento, entrega de pedidos e giro de salão através de câmeras de segurança comuns (CFTV/RTSP).

---

## 📚 Documentação do Projeto

* 📄 **[DOCUMENTACAO.md](DOCUMENTACAO.md):** Guia técnico completo, diretrizes de segurança contra SQL Injection, como executar cada script e **changelog de features atualizado**.
* 📋 **[PLANEJAMENTO.md](PLANEJAMENTO.md):** Planejamento estratégico, máquina de estados (FSM), regras de negócio e roadmap em 8 fases.

---

## 🚀 Como Iniciar

1. **Ative o ambiente virtual:**
   ```powershell
   .\.venv\Scripts\activate
   ```

2. **Inspecione o vídeo de exemplo:**
   ```powershell
   python src\test_video.py
   ```

3. **Valide a detecção YOLO:**
   ```powershell
   python src\test_yolo.py
   ```

4. **Calibre ou gere os polígonos das mesas:**
   ```powershell
   python src\gerar_calibracao.py
   ```

5. **Execute o Motor de Detecção de Ocupação:**
   ```powershell
   python src\motor_ocupacao.py
   ```

6. **Execute o Motor de Estados com Cronômetros e Alertas:**
   ```powershell
   python src\motor_estados.py
   ```

7. **Abra o Dashboard Web Gerencial (Streamlit):**
   ```powershell
   streamlit run src\dashboard.py
   ```

8. **Teste a Ingestão Resiliente de Câmeras (RTSP / CFTV):**
   ```powershell
   python src\camera_stream.py
   ```

9. **Implantação Rápida em Servidor / Mini PC (Docker):**
   ```bash
   docker compose up -d
   ```
   *Ou execute o instalador em 1 comando no Linux:*
   ```bash
   sudo bash install.sh
   ```
Te