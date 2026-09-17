from pydantic import BaseModel, Field


# 生成解说片段
class ScriptSegment(BaseModel):
    page: int = Field(
        description="播放当前解说内容时应该展示的 PDF 页码"
    )

    text: str = Field(
        description="当前这一段的视频中文解说词"
    )

class PaperScript(BaseModel):
    title: str = Field(
        description="视频标题"
    )

    segments: list[ScriptSegment] = Field(
        description="按解说顺序排列的视频解说段落"
    )