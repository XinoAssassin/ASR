const zhCN = {
  app: {
    title: '多说话人语音转写',
  },
  nav: {
    home: '首页',
    upload: '上传',
    jobs: '任务',
    settings: '设置',
  },
  health: {
    title: '后端状态',
    checking: '正在连接…',
    connected: '已连接',
    error: '无法连接后端',
    retry: '重试',
    backendUrl: '后端地址',
    sameOrigin: '同源（开发代理）',
    version: '版本',
    decoder: '解码器',
    ffmpeg: 'FFmpeg',
    apiKeyRequired: '需要 API Key',
    yes: '是',
    no: '否',
  },
  placeholder: {
    comingSoon: '即将推出',
  },
  views: {
    upload: { title: '上传音频' },
    jobs: { title: '转写任务' },
    transcript: { title: '转写结果' },
    settings: { title: '设置' },
    notFound: { title: '页面不存在', back: '返回首页' },
  },
  jobStatus: {
    queued: '排队中',
    running: '进行中',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
  },
  stage: {
    queued: '排队中',
    preparing: '准备音频',
    uploading: '上传中',
    cloud_queued: '云端排队',
    recognizing: '识别中',
    finalizing: '整理结果',
    done: '完成',
    decoding: '解码',
    vad: '语音检测',
    asr: '语音识别',
    aligning: '时间对齐',
    diarizing: '说话人分离',
    merging: '合并结果',
  },
}

export default zhCN
export type MessageSchema = typeof zhCN
