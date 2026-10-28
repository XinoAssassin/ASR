import type { MessageSchema } from './zh-CN'

const en: MessageSchema = {
  app: {
    title: 'Multi-Speaker Transcription',
  },
  nav: {
    home: 'Home',
    upload: 'Upload',
    jobs: 'Jobs',
    settings: 'Settings',
  },
  health: {
    title: 'Backend status',
    checking: 'Connecting…',
    connected: 'Connected',
    error: 'Cannot reach the backend',
    retry: 'Retry',
    backendUrl: 'Backend URL',
    sameOrigin: 'Same origin (dev proxy)',
    version: 'Version',
    decoder: 'Decoder',
    ffmpeg: 'FFmpeg',
    apiKeyRequired: 'API key required',
    yes: 'Yes',
    no: 'No',
  },
  placeholder: {
    comingSoon: 'Coming soon',
  },
  views: {
    upload: { title: 'Upload audio' },
    jobs: { title: 'Jobs' },
    transcript: { title: 'Transcript' },
    settings: { title: 'Settings' },
    notFound: { title: 'Page not found', back: 'Back to home' },
  },
  jobStatus: {
    queued: 'Queued',
    running: 'Running',
    succeeded: 'Succeeded',
    failed: 'Failed',
    cancelled: 'Cancelled',
  },
  stage: {
    queued: 'Queued',
    preparing: 'Preparing audio',
    uploading: 'Uploading',
    cloud_queued: 'Queued in cloud',
    recognizing: 'Recognizing',
    finalizing: 'Finalizing',
    done: 'Done',
    decoding: 'Decoding',
    vad: 'Voice activity detection',
    asr: 'Speech recognition',
    aligning: 'Aligning',
    diarizing: 'Diarizing',
    merging: 'Merging',
  },
}

export default en
