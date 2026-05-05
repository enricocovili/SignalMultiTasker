import bridge

with open('/app/test.wav','rb') as f:                                                     
        txt = bridge.transcribe_audio(f.read(), 'test.wav')                                   
print('TRANSCRIPT:', txt)                                                                 
print('SUMMARY:', bridge.get_llm_summary(txt, prompt_template=bridge.VOICE_LLM_PROMPT))
