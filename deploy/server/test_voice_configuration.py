"""Offline regression: external ASR configuration and transport boundaries.
Run: python -B -m unittest deploy.server.test_voice_configuration -v
No database, real provider, or private configuration is used.
"""
import os,sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from deploy.server import settings
import voice_transcription as voice

CONFIG={'files':{k:'unused-test-file' for k in ['model_environment','totp_key','payment_key','seat_credentials','budget_config','memory_client','memory_fence']},'database':{'host':'db.test.invalid','port':5432,'name':'test_db','role':'test_role','passfile':'/tmp/no-file-read'},'mobile_state_dir':'/tmp/test-voice-state','bank_origin':'https://bank.test.invalid'}
ASR={'BA_ASR_ENABLED':'1','BA_ASR_ENDPOINT':'https://asr.test.invalid/v1/audio/transcriptions','BA_ASR_MODEL':'test-model','BA_ASR_API_KEY':'test-secret','BA_ASR_MIME_TYPES':'audio/webm,audio/mp4'}
class VoiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{},clear=True);self.env.start();self.addCleanup(self.env.stop)
    def install(self,values):
        with patch.object(settings,'environment_file',return_value=values):settings.install(CONFIG)
    def test_external_asr_settings_survive_startup(self):
        self.install(ASR);self.assertTrue(voice.capabilities()['configured']);self.assertEqual(os.getenv('BA_ASR_API_KEY'),'test-secret')
    def test_missing_asr_stays_disabled(self):
        self.install({});self.assertEqual(voice.capabilities()['reason'],'voice_not_configured')
    def test_untrusted_container_asr_does_not_survive(self):
        os.environ.update(ASR);self.install({});self.assertFalse(voice.capabilities()['configured'])
    def test_arbitrary_ba_flags_cannot_disable_auth(self):
        self.install({**ASR,'BA091_AUTH_MODE':'disabled','BA_PAYMENT_AUTH_ENABLED':'0','BA_ASR_UNREVIEWED':'bad'})
        self.assertEqual(os.getenv('BA091_AUTH_MODE'),'secure');self.assertEqual(os.getenv('BA_PAYMENT_AUTH_ENABLED'),'1');self.assertNotIn('BA_ASR_UNREVIEWED',os.environ)
    def test_provider_credentials_not_exposed_in_capabilities(self):
        self.install(ASR);self.assertNotIn('test-secret',str(voice.capabilities()));self.assertNotIn('asr.test.invalid',str(voice.capabilities()))
    def test_public_plain_http_rejected(self):
        self.install({**ASR,'BA_ASR_ENDPOINT':'http://asr.test.invalid/transcribe'});self.assertEqual(voice.capabilities()['reason'],'voice_configuration_invalid')
    def test_ios_mp4_and_webm_are_accepted(self):
        self.install(ASR);config=voice.configuration()
        self.assertEqual(voice.validate_audio(b'\0\0\0\x18ftypisom\0\0','audio/mp4',1000,config),'audio/mp4')
        self.assertEqual(voice.validate_audio(b'\x1aE\xdf\xa3test','audio/webm;codecs=opus',1000,config),'audio/webm')
    def test_fake_audio_payload_rejected(self):
        self.install(ASR)
        with self.assertRaises(voice.VoiceError):voice.validate_audio(b'not audio','audio/mp4',1000,voice.configuration())
    def test_existing_model_settings_are_unchanged(self):
        model={'LLM_MODEL':'existing-chat','VISION_MODEL':'existing-vision','R4_TARGET_MODEL':'existing-memory'};self.install({**ASR,**model})
        for k,v in model.items():self.assertEqual(os.environ[k],v)
if __name__=='__main__':unittest.main()
