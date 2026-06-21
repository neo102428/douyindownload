#!/usr/bin/env python3
"""
调试脚本：检查已下载目录中的作品数据结构
用于分析为什么多张动图作品没有被识别
"""
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent

def debug_downloaded_items(output_dir):
    """分析已下载目录中的作品"""
    output_path = Path(output_dir)

    if not output_path.exists():
        print(f"❌ 目录不存在: {output_dir}")
        return

    print(f"正在分析目录: {output_dir}")

    # 统计文件类型
    mp4_files = list(output_path.glob("*.mp4"))
    video_json_files = list(output_path.glob("*.video.json"))
    item_json_files = list(output_path.glob(".douyin_item.json"))

    print(f"\n文件统计:")
    print(f"  MP4文件: {len(mp4_files)}")
    print(f"  .video.json文件: {len(video_json_files)}")
    print(f"  .douyin_item.json文件: {len(item_json_files)}")

    # 分析 .douyin_item.json 文件
    if item_json_files:
        print(f"\n分析 .douyin_item.json 文件:")
        for item_file in item_json_files[:3]:  # 只分析前3个
            try:
                data = json.loads(item_file.read_text(encoding="utf-8"))
                print(f"\n  文件: {item_file.parent.name}")
                print(f"  作品ID: {data.get('aweme_id')}")
                print(f"  作品类型: {data.get('kind')}")
                print(f"  图片数量: {data.get('image_count')}")
                print(f"  动图数量: {data.get('motion_count')}")
            except Exception as e:
                print(f"  ❌ 读取失败: {e}")

    # 分析 .video.json 文件
    if video_json_files:
        print(f"\n分析 .video.json 文件:")
        for video_file in video_json_files[:3]:  # 只分析前3个
            try:
                data = json.loads(video_file.read_text(encoding="utf-8"))
                print(f"\n  文件: {video_file.name}")
                print(f"  作品ID: {data.get('aweme_id')}")
                print(f"  标题: {data.get('title', '')[:50]}")
                print(f"  发布时间: {data.get('publish_time')}")
            except Exception as e:
                print(f"  ❌ 读取失败: {e}")

    # 查找manifest.csv
    manifest_path = output_path / "manifest.csv"
    if manifest_path.exists():
        print(f"\n找到manifest.csv，正在分析...")
        try:
            import csv
            with manifest_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

                print(f"  总作品数: {len(rows)}")

                # 统计模式
                mode_counts = {}
                for row in rows:
                    mode = row.get("detected_mode", "unknown")
                    mode_counts[mode] = mode_counts.get(mode, 0) + 1

                print(f"  模式分布:")
                for mode, count in mode_counts.items():
                    print(f"    {mode}: {count}")

                # 查找douyin_media模式的作品
                media_items = [row for row in rows if row.get("detected_mode") == "douyin_media"]
                if media_items:
                    print(f"\n  找到 {len(media_items)} 个douyin_media模式作品:")
                    for item in media_items[:3]:
                        print(f"    - ID: {item.get('aweme_id')}, 标题: {item.get('title', '')[:30]}")
        except Exception as e:
            print(f"  ❌ 读取manifest.csv失败: {e}")

def main():
    if len(sys.argv) < 2:
        print("用法: python3 debug_author_api.py <已下载目录>")
        print("示例: python3 debug_author_api.py downloads/吃啥好呢烦")
        print("\n或者直接分析当前目录下的所有downloads目录:")
        print("python3 debug_author_api.py --all")

        # 如果没有参数，显示可用的目录
        downloads_dir = WORKSPACE / "downloads"
        if downloads_dir.exists():
            print(f"\n可用的下载目录:")
            for d in downloads_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    print(f"  - {d.name}")
        return

    if sys.argv[1] == "--all":
        downloads_dir = WORKSPACE / "downloads"
        if downloads_dir.exists():
            for d in downloads_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    print("=" * 60)
                    debug_downloaded_items(d)
                    print()
    else:
        output_dir = sys.argv[1]
        debug_downloaded_items(output_dir)

if __name__ == "__main__":
    main()
