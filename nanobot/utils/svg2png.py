# -*- coding: utf-8 -*-
# =====================
# SVG转PNG工具
# Author: AI Assistant
# Date: 2025/1/27
# =====================

import os
import sys
import base64
import ctypes
import ctypes.util
import tempfile
import subprocess
from typing import Optional
from loguru import logger

# ---------------------------------------------------------------------------
# 自动发现 & 预加载 homebrew cairo 库
# macOS + pyenv/venv 下 cairocffi 的 dlopen 无法自动找到 brew 安装的 libcairo，
# 需要在 import cairocffi 之前通过 ctypes 显式预加载共享库。
# ---------------------------------------------------------------------------
def _preload_cairo_lib() -> bool:
    """在 cairocffi import 之前预加载 libcairo（解决 macOS SIP + brew 路径问题）。"""
    # 如果 ctypes 已经能找到，不需要额外处理
    if ctypes.util.find_library("cairo"):
        return True

    if sys.platform != "darwin":
        return False

    # 尝试通过 brew 找到 cairo 的安装路径
    candidates: list[str] = []
    try:
        prefix = subprocess.check_output(
            ["brew", "--prefix", "cairo"], stderr=subprocess.DEVNULL
        ).decode().strip()
        lib_dir = os.path.join(prefix, "lib")
        if os.path.isdir(lib_dir):
            candidates.append(os.path.join(lib_dir, "libcairo.2.dylib"))
            candidates.append(os.path.join(lib_dir, "libcairo.dylib"))
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # 也检查常见的 homebrew 路径
    for brew_prefix in ["/opt/homebrew/opt/cairo/lib", "/usr/local/opt/cairo/lib"]:
        if os.path.isdir(brew_prefix):
            candidates.append(os.path.join(brew_prefix, "libcairo.2.dylib"))
            candidates.append(os.path.join(brew_prefix, "libcairo.dylib"))

    for lib_path in candidates:
        if os.path.isfile(lib_path):
            try:
                ctypes.cdll.LoadLibrary(lib_path)
                logger.info(f"已预加载 cairo 库: {lib_path}")
                return True
            except OSError as e:
                logger.debug(f"预加载 {lib_path} 失败: {e}")

    return False

_preload_cairo_lib()

try:
    from cairosvg import svg2png
    CAIROSVG_AVAILABLE = True
except (ImportError, OSError):
    CAIROSVG_AVAILABLE = False
    logger.warning("cairosvg not available, using fallback method")

try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("PIL not available, using fallback method")

try:
    from genie_tool.util.file_util import upload_file, upload_file_by_path
    GENIE_TOOL_AVAILABLE = True
except ImportError:
    GENIE_TOOL_AVAILABLE = False
    upload_file = None
    upload_file_by_path = None
    logger.warning("genie_tool not available, svg_to_png_and_upload will not work")


def fix_svg(svg_content: str) -> str:
    """
    修复SVG中的常见语法错误（供外部校验流程使用）。
    仅做语法修复，不进行转换。
    """
    return _fix_svg_syntax(svg_content)


def svg_to_png_base64(
    svg_content: str,
    width: int = 500,
    height: int = 500,
    apply_fix: bool = True,
) -> Optional[str]:
    """
    将SVG内容转换为PNG的Base64编码（仅用于校验时可不保存PNG）。

    Args:
        svg_content: SVG内容字符串
        width: 输出PNG宽度，默认500
        height: 输出PNG高度，默认500
        apply_fix: 是否先调用 fix_svg 修复语法，默认 True。设为 False 时可先校验原始内容。

    Returns:
        PNG的Base64编码字符串，失败返回None
    """
    try:
        if apply_fix:
            svg_content = _fix_svg_syntax(svg_content)

        if CAIROSVG_AVAILABLE:
            return _svg_to_png_cairosvg(svg_content, width, height)
        elif PIL_AVAILABLE:
            return _svg_to_png_pil_fallback(svg_content, width, height)
        else:
            return _svg_to_png_simple_fallback(svg_content, width, height)
    except Exception as e:
        logger.error(f"SVG转PNG失败: {e}")
        import traceback
        logger.error(f"错误堆栈: {traceback.format_exc()}")
        logger.error(f"问题SVG内容（前500字符）: {svg_content[:500]}")
        return None


def _fix_svg_syntax(svg_content: str) -> str:
    """
    修复SVG中的常见语法错误
    
    Args:
        svg_content: 原始SVG内容
        
    Returns:
        修复后的SVG内容
    """
    import re
    
    try:
        original_content = svg_content
        
        # 1. 修复<line>标签中重复的属性（常见LLM错误）
        # 错误示例1：x1 y2 x2 y2 → 应该是 x1 y1 x2 y2
        # 错误示例2：x1 y1 y2 y2 → 应该是 x1 y1 x2 y2
        # 错误示例3：x1 y1 x1 y1 → 应该是 x1 y1 x2 y2（新发现）
        # 错误示例4：x1 y1 x2 y1 x2 y2 → 应该是 x1 y1 x2 y2（新发现）
        def fix_line_attributes(match):
            line_tag = match.group(0)
            original_tag = line_tag
            fixed = False
            
            # 提取所有属性
            attrs = re.findall(r'(\w+)="([^"]*)"', line_tag)
            attr_dict = {}
            for attr_name, attr_value in attrs:
                if attr_name not in attr_dict:
                    attr_dict[attr_name] = []
                attr_dict[attr_name].append(attr_value)
            
            # 检查是否有重复的x1和y1，但没有x2和y2（错误：x1 y1 x1 y1）
            if len(attr_dict.get('x1', [])) >= 2 and len(attr_dict.get('y1', [])) >= 2:
                if 'x2' not in attr_dict and 'y2' not in attr_dict:
                    # 将第二组x1和y1替换为x2和y2
                    # 使用更精确的替换策略
                    count = [0, 0]  # [x1_count, y1_count]
                    
                    def replace_second_occurrence(match_obj):
                        attr = match_obj.group(1)
                        if attr == 'x1':
                            count[0] += 1
                            if count[0] == 2:
                                return 'x2='
                        elif attr == 'y1':
                            count[1] += 1
                            if count[1] == 2:
                                return 'y2='
                        return match_obj.group(0)
                    
                    line_tag = re.sub(r'\b(x1|y1)=', replace_second_occurrence, line_tag)
                    logger.info(f"修复line标签：将第二组x1/y1替换为x2/y2（x1 y1 x1 y1模式）")
                    fixed = True
            
            # 如果上面的修复已经处理了，就不需要继续处理了
            if not fixed:
                # 检查是否有两个y2但没有y1（错误：x1 y2 x2 y2）
                if line_tag.count('y2=') >= 2 and 'y1=' not in line_tag:
                    # 找到第一个y2，替换为y1
                    line_tag = re.sub(r'\by2=', 'y1=', line_tag, count=1)
                    logger.info(f"修复line标签：将第一个y2替换为y1")
                    fixed = True
                
                # 检查是否有两个y2但没有x2（错误：x1 y1 y2 y2）
                # 需要更智能的判断：如果有x1和y1，但没有x2，且有两个y2
                if line_tag.count('y2=') >= 2 and 'x2=' not in line_tag:
                    # 检查模式：x1 y1 y2 y2
                    if 'x1=' in line_tag and 'y1=' in line_tag:
                        # 第一个y2应该是x2
                        line_tag = re.sub(r'\by2=', 'x2=', line_tag, count=1)
                        logger.info(f"修复line标签：将第一个y2替换为x2（缺少x2的情况）")
                        fixed = True
                
                # 检查是否有两个x2但没有x1
                if line_tag.count('x2=') >= 2 and 'x1=' not in line_tag:
                    # 找到第一个x2，替换为x1
                    line_tag = re.sub(r'\bx2=', 'x1=', line_tag, count=1)
                    logger.info(f"修复line标签：将第一个x2替换为x1")
                    fixed = True
                
                # 检查是否有两个x2但没有y2（错误：x1 y1 x2 x2）
                if line_tag.count('x2=') >= 2 and 'y2=' not in line_tag:
                    # 检查模式：x1 y1 x2 x2
                    if 'x1=' in line_tag and 'y1=' in line_tag:
                        # 第二个x2应该是y2
                        # 先找到所有x2的位置
                        x2_matches = list(re.finditer(r'\bx2=', line_tag))
                        if len(x2_matches) >= 2:
                            # 替换第二个x2为y2
                            second_x2_pos = x2_matches[1].start()
                            line_tag = line_tag[:second_x2_pos] + 'y2=' + line_tag[second_x2_pos+3:]
                            logger.info(f"修复line标签：将第二个x2替换为y2（缺少y2的情况）")
                            fixed = True
            
            # 检查是否有属性混乱的情况（如：x1 y1 x2 y1 x2 y2）
            # 这种情况需要删除重复的中间属性
            if not fixed:
                # 重新提取属性（因为可能已经被修复过）
                attrs = re.findall(r'(\w+)="([^"]*)"', line_tag)
                seen_attrs = {}
                clean_attrs = []
                
                for attr_name, attr_value in attrs:
                    # 只保留每个属性的第一次出现（除了style等可能重复的属性）
                    if attr_name in ['x1', 'y1', 'x2', 'y2']:
                        if attr_name not in seen_attrs:
                            seen_attrs[attr_name] = attr_value
                            clean_attrs.append((attr_name, attr_value))
                        else:
                            # 如果是重复属性，可能是错误，跳过
                            logger.info(f"修复line标签：删除重复属性 {attr_name}=\"{attr_value}\"")
                            fixed = True
                    else:
                        clean_attrs.append((attr_name, attr_value))
                
                if fixed:
                    # 重建line标签
                    attrs_str = ' '.join([f'{name}="{value}"' for name, value in clean_attrs])
                    # 提取其他非属性部分（如class等）
                    tag_parts = re.split(r'\s+\w+="[^"]*"', original_tag)
                    line_tag = f'<line {attrs_str}'
                    # 保留原标签的结尾部分
                    if original_tag.endswith('/>'):
                        line_tag += '/>'
                    else:
                        line_tag += '>'
            
            # if fixed:
            #     # 输出修复前后的对比（仅在DEBUG模式下）
            #     try:
            #         logger.debug(f"修复前: {original_tag}")
            #         logger.debug(f"修复后: {line_tag}")
            #     except:
            #         pass
            
            return line_tag
        
        svg_content = re.sub(r'<line[^>]*>', fix_line_attributes, svg_content, flags=re.IGNORECASE)
        
        # 2. 修复自闭合标签中的多余空格
        svg_content = re.sub(r'\s+/>', '/>', svg_content)
        
        # 3. 修复属性值中的多余空格
        svg_content = re.sub(r'=\s+"', '="', svg_content)
        svg_content = re.sub(r'=\s+"', '="', svg_content)
        
        # 4. 确保SVG标签有xmlns属性（如果没有的话）
        if '<svg' in svg_content and 'xmlns=' not in svg_content:
            svg_content = re.sub(
                r'<svg\s+',
                '<svg xmlns="http://www.w3.org/2000/svg" ',
                svg_content,
                count=1
            )
            logger.info("添加xmlns属性到SVG标签")
        
        # 5. 修复use标签的href属性（某些SVG使用xlink:href，确保兼容性）
        if '<use' in svg_content and 'href=' in svg_content:
            # 检查是否同时存在xmlns:xlink
            if 'xmlns:xlink=' not in svg_content:
                # 在svg标签中添加xmlns:xlink
                svg_content = re.sub(
                    r'(<svg[^>]*xmlns="[^"]*")',
                    r'\1 xmlns:xlink="http://www.w3.org/1999/xlink"',
                    svg_content,
                    count=1
                )
                logger.info("添加xmlns:xlink属性到SVG标签")
        
        # if svg_content != original_content:
        #     logger.info("SVG语法已修复，部分错误已自动更正")
        #     # 输出修复前后的差异（用于调试）
        #     try:
        #         logger.debug(f"修复前SVG（前300字符）: {original_content[:10]}")
        #         logger.debug(f"修复后SVG（前300字符）: {svg_content[:10]}")
        #     except:
        #         pass
        
        return svg_content
        
    except Exception as e:
        logger.warning(f"SVG语法修复失败（将使用原内容）: {e}")
        return svg_content


def _preprocess_svg_for_chinese(svg_content: str) -> str:
    """
    预处理SVG内容，确保中文字符能正确显示
    
    Args:
        svg_content: 原始SVG内容
        
    Returns:
        处理后的SVG内容
    """
    import re
    
    # 使用系统已安装的阿里巴巴普惠体字体
    font_family = "'Alibaba PuHuiTi 3.0', sans-serif"
    
    # 检查SVG是否已经有<style>标签
    if '<style' in svg_content.lower():
        # 如果有style标签，在其中添加字体设置
        style_pattern = r'(<style[^>]*>)(.*?)(</style>)'
        
        def add_font_to_style(match):
            style_open = match.group(1)
            style_content = match.group(2)
            style_close = match.group(3)
            
            # 添加通用的字体设置
            font_css = f"""
                /* 中文字体支持 */
                text, tspan {{
                    font-family: {font_family};
                }}
            """
            
            return f"{style_open}{font_css}{style_content}{style_close}"
        
        svg_content = re.sub(style_pattern, add_font_to_style, svg_content, flags=re.DOTALL | re.IGNORECASE)
    else:
        # 如果没有style标签，在<svg>标签后添加
        svg_tag_pattern = r'(<svg[^>]*>)'
        
        def add_style_tag(match):
            svg_tag = match.group(1)
            style_tag = f"""
    <defs>
        <style type="text/css">
            /* 中文字体支持 */
            text, tspan {{
                font-family: {font_family};
            }}
        </style>
    </defs>
"""
            return f"{svg_tag}{style_tag}"
        
        svg_content = re.sub(svg_tag_pattern, add_style_tag, svg_content, flags=re.IGNORECASE)
    
    # 如果SVG中的<text>标签有font-family属性且不支持中文，替换它
    # 匹配常见的英文字体
    english_fonts = ['Arial', 'Times New Roman', 'Helvetica', 'Verdana', 'Georgia', 'Courier']
    for eng_font in english_fonts:
        # 替换font-family属性中的英文字体
        svg_content = re.sub(
            rf'font-family\s*[:=]\s*["\']?{eng_font}["\']?',
            f'font-family="{font_family}"',
            svg_content,
            flags=re.IGNORECASE
        )
    return svg_content


def _svg_to_png_cairosvg(svg_content: str, width: int, height: int) -> Optional[str]:
    """使用cairosvg库转换SVG到PNG（支持中文字符）"""
    try:
        # 预处理SVG内容，确保中文字体设置
        processed_svg = _preprocess_svg_for_chinese(svg_content)
        
        # 创建临时文件
        with tempfile.NamedTemporaryFile(suffix='.svg', delete=False, mode='w', encoding='utf-8') as svg_file:
            svg_file.write(processed_svg)
            svg_path = svg_file.name
        
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as png_file:
            png_path = png_file.name
        
        try:
            # 转换SVG到PNG
            svg2png(url=svg_path, write_to=png_path, output_width=width, output_height=height)
            
            # 读取PNG文件并转换为Base64
            with open(png_path, 'rb') as f:
                png_data = f.read()
            
            logger.info(f"✅ SVG转PNG成功（支持中文），大小: {len(png_data)} bytes")
            return base64.b64encode(png_data).decode('utf-8')
            
        finally:
            # 清理临时文件
            try:
                os.unlink(svg_path)
            except:
                pass
            try:
                os.unlink(png_path)
            except:
                pass
        
    except Exception as e:
        logger.error(f"cairosvg转换失败: {e}")
        return None


def _get_chinese_font():
    """
    获取支持中文的字体（使用系统已安装的阿里巴巴普惠体）
    
    Returns:
        ImageFont对象，如果找不到则返回默认字体
    """
    try:
        from PIL import ImageFont
        
        # 使用系统已安装的阿里巴巴普惠体字体
        font_path = '/usr/share/fonts/AlibabaPuHuiTi-3-55-Regular/AlibabaPuHuiTi-3-55-Regular.ttf'
        
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, size=20)
                logger.info(f"✅ 使用中文字体: {font_path}")
                return font
            except Exception as e:
                logger.error(f"加载字体失败 {font_path}: {e}")
                return ImageFont.load_default()
        else:
            logger.error(f"字体文件不存在: {font_path}")
            return ImageFont.load_default()
        
    except Exception as e:
        logger.error(f"获取中文字体失败: {e}")
        return None


def _svg_to_png_pil_fallback(svg_content: str, width: int, height: int) -> Optional[str]:
    """使用PIL创建简单的PNG作为fallback（支持中文）"""
    try:
        # 创建一个白色背景的PNG图像
        image = Image.new('RGB', (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        
        # 绘制边框
        draw.rectangle([5, 5, width-5, height-5], outline=(0, 0, 0), width=2)
        
        # 绘制一些基本图形来模拟SVG内容
        center_x, center_y = width // 2, height // 2
        
        # 绘制圆形
        circle_radius = min(width, height) // 6
        draw.ellipse([center_x - circle_radius, center_y - circle_radius, 
                     center_x + circle_radius, center_y + circle_radius], 
                    outline=(0, 0, 255), width=2)
        
        # 绘制线条
        draw.line([10, 10, width-10, height-10], fill=(255, 0, 0), width=2)
        draw.line([width-10, 10, 10, height-10], fill=(0, 255, 0), width=2)
        
        # 添加中文文字
        try:
            # 获取支持中文的字体
            font = _get_chinese_font()
            
            # 添加文字（支持中文）
            text = "SVG图形"
            if font:
                # 计算文本位置（居中）
                try:
                    # 使用textbbox获取文本边界框（PIL 8.0.0+）
                    bbox = draw.textbbox((0, 0), text, font=font)
                    text_width = bbox[2] - bbox[0]
                    text_height = bbox[3] - bbox[1]
                except AttributeError:
                    # 旧版本PIL使用textsize
                    text_width, text_height = draw.textsize(text, font=font)
                
                text_x = center_x - text_width // 2
                text_y = center_y + circle_radius + 20
                
                draw.text((text_x, text_y), text, fill=(0, 0, 0), font=font)
                logger.info("✅ PIL fallback已添加中文文字")
            else:
                # 如果没有字体，使用英文
                draw.text((center_x-20, center_y+circle_radius+10), "SVG", fill=(0, 0, 0))
                logger.warning("⚠️ PIL fallback使用默认字体（可能不支持中文）")
        except Exception as text_error:
            logger.warning(f"添加文字失败: {text_error}")
            pass
        
        # 转换为Base64
        import io
        buffer = io.BytesIO()
        image.save(buffer, format='PNG', optimize=True)
        png_data = buffer.getvalue()
        
        return base64.b64encode(png_data).decode('utf-8')
        
    except Exception as e:
        logger.error(f"PIL fallback转换失败: {e}")
        return None


def _svg_to_png_simple_fallback(svg_content: str, width: int, height: int) -> Optional[str]:
    """最简单的fallback方法，返回一个1x1透明PNG"""
    try:
        # 返回一个1x1透明PNG的Base64编码
        return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    except Exception as e:
        logger.error(f"简单fallback转换失败: {e}")
        return None


async def svg_to_png_and_upload(svg_content: str, request_id: str, session_id: Optional[str] = None, width: int = 500, height: int = 500) -> Optional[dict]:
    """
    将SVG转换为PNG并上传到文件服务器
    
    Args:
        svg_content: SVG内容字符串
        request_id: 请求ID
        session_id: 会话ID，用于生成file_id
        width: 输出PNG宽度，默认500
        height: 输出PNG高度，默认500
    
    Returns:
        包含文件信息的字典，包含ossUrl、domainUrl等，失败返回None
    """
    try:
        if not session_id:
            logger.error(f"svg_to_png_and_upload: session_id is required, requestId={request_id}")
            return None
            
        # 转换SVG到PNG Base64
        png_base64 = svg_to_png_base64(svg_content, width, height)
        if not png_base64:
            logger.error("SVG转PNG失败")
            return None
        
        # 解码Base64为二进制数据
        png_data = base64.b64decode(png_base64)
        
        # 生成文件名
        import time
        timestamp = int(time.time() * 1000)
        file_name = f"svg_generated_{timestamp}.png"
        
        # 创建临时文件
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
            temp_file.write(png_data)
            temp_path = temp_file.name
        
        try:
            # 使用upload_file_by_path上传二进制PNG文件
            result = await upload_file_by_path(
                file_path=temp_path,
                request_id=request_id,
                session_id=session_id
            )
            
            return result
            
        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.unlink(temp_path)
                
    except Exception as e:
        logger.error(f"SVG转PNG并上传失败: {e}")
        return None


def extract_svg_content(svg_text: str) -> Optional[str]:
    """
    从文本中提取SVG标签内容
    
    Args:
        svg_text: 包含SVG标签的文本
    
    Returns:
        提取的SVG内容，失败返回None
    """
    try:
        import re
        
        # 匹配<svg>...</svg>标签
        pattern = r'<svg[^>]*>(.*?)</svg>'
        match = re.search(pattern, svg_text, re.DOTALL | re.IGNORECASE)
        
        if match:
            return match.group(0)  # 返回完整的SVG标签
        else:
            logger.warning("未找到有效的SVG标签")
            return None
            
    except Exception as e:
        logger.error(f"提取SVG内容失败: {e}")
        return None


async def main():
    """命令行入口函数"""
    import argparse
    import asyncio
    
    parser = argparse.ArgumentParser(description='SVG转PNG工具')
    parser.add_argument('--svg_file', required=True, help='SVG文件路径')
    parser.add_argument('--request_id', required=True, help='请求ID')
    parser.add_argument('--width', type=int, default=500, help='输出PNG宽度')
    parser.add_argument('--height', type=int, default=500, help='输出PNG高度')
    
    args = parser.parse_args()
    
    try:
        # 读取SVG文件内容
        with open(args.svg_file, 'r', encoding='utf-8') as f:
            svg_content = f.read()
        
        # 转换并上传
        result = await svg_to_png_and_upload(
            svg_content=svg_content,
            request_id=args.request_id,
            width=args.width,
            height=args.height
        )
        
        if result:
            # 输出结果URL
            print(result.get('ossUrl', ''))
        else:
            print("")
            exit(1)
            
    except Exception as e:
        logger.error(f"命令行执行失败: {e}")
        print("")
        exit(1)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
