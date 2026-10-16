"""Small CLI over the same persistent application service used by tests."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from filelock import Timeout
from pydantic import ValidationError

from algo_coach.calendar import parse_date, today
from algo_coach.problems import LocalProblems
from algo_coach.runner import DockerRunner
from algo_coach.service import MAX_CODE_BYTES, Coach
from algo_coach.teaching import DemoTeacher, ModelTeacher
from algo_coach.verification import VerificationService, render_report

HELP = """会话命令：
  /hint                 逐级提示，最高 H3
  /answer               明确请求完整解答，允许 H4
  /code 文件路径        保存 Python 文件并检查语法（不执行）
  /code                 粘贴多行代码，以独立一行 .end 结束，.cancel 取消
  /test                 在配置好的 Docker 执行器中验证已保存代码，不调用模型
  /finish               进入复盘阶段
  /note 反思内容        保存用户自己的反思
  /explain 思路内容     保存当前代码的用户思路
  /summarize            调用模型整理现有代码，保存待审阅题解草稿
  /complete 完成程度    independent / hint_assisted / answer_assisted / unfinished
  /archive              生成练习目录归档预览
  /archive show 文件名  查看预览中的文件内容
  /archive apply        写入已预览且未过期的归档
  /commit               预览本地提交；/commit apply 明确执行
  /push                 预览远端推送；/push apply 明确执行（需配置策略）
  /problem 题号或链接   开始另一道本地题，保留原会话
  /fetch 题号或链接     从中国站获取或读取缓存，并开始新会话
  /fetch --offline 25   只读远程缓存，不请求题源
  /daily fetch [日期]   获取中国站每日题并开始新会话
  /daily [YYYY-MM-DD]   学习指定日期登记的题目（默认上海日历今天）
  /review              查看到期复习题目
  /review add          把当前题加入复习，首次安排在明天
  /review start 标识   用全新会话复习，如 leetcode_cn:25
  /review rate 结果    本次复习自评 again / hard / good
  /status               查看阶段、代码版本和验证状态
  /retry                重试上次失败回复，不额外升级提示
  /help                 显示本说明
  /quit                 退出，下次用 resume 恢复
远程仅支持中国站普通数字题号、公开 Solution 单方法题。动态验证仅支持已审核题面；Git 写操作默认禁用。
"""


def source_options(item, *, select=False):
    if select:
        item.add_argument("--remote", action="store_true", help="显式使用中国站及独立远程缓存")
    options = item.add_mutually_exclusive_group()
    options.add_argument("--offline", action="store_true", help="只读已有远程缓存，不请求题源")
    options.add_argument("--refresh", action="store_true", help="强制刷新；失败时保留旧缓存并报错")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AlgoCoach 本地交互式算法教练")
    root.add_argument("--demo", action="store_true", help="显式使用离线脚本演示，不调用模型")
    root.add_argument("--data-dir", type=Path, default=None, help="学习记录目录")
    root.add_argument("--practice-dir", type=Path, default=None, help="独立练习目录，需已存在")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("problems", help="列出本地题库")
    sub.add_parser("sessions", help="列出历史会话")
    sub.add_parser("doctor", help="检查 Docker 与执行器镜像，不调用模型")
    item = sub.add_parser("fetch", help="获取中国站题目或读取独立远程缓存，不调用模型")
    item.add_argument("reference", help="数字题号、leetcode25 或中国站原题链接")
    source_options(item)
    daily = sub.add_parser("daily", help="登记、查看或学习按日期保存的题目")
    actions = daily.add_subparsers(dest="daily_action", required=True)
    for name in ("set", "show", "chat", "fetch"):
        item = actions.add_parser(name)
        item.add_argument("--date", type=parse_date, default=None, help="YYYY-MM-DD，默认上海今天")
        item.add_argument(
            "--site", choices=["leetcode_cn", "leetcode_com", "local"], default="leetcode_cn"
        )
        if name == "set":
            item.add_argument("reference", help="本地题号或原题链接；可先 import 导入")
            item.add_argument(
                "--platform",
                action="store_true",
                help="声明用户已在原站核对平台每日题，工具未自动核实",
            )
            item.add_argument("--source-url", default="", help="--platform 所需的原题来源链接")
            item.add_argument(
                "--replace", action="store_true", help="明确替换该站点该日期已有的不同记录"
            )
        else:
            source_options(item, select=name != "fetch")
    review = sub.add_parser("review", help="查看到期题目、安排复习或开始新会话")
    actions = review.add_subparsers(dest="review_action", required=True)
    item = actions.add_parser("list")
    item.add_argument(
        "--date", type=parse_date, default=None, help="查询截至 YYYY-MM-DD 到期的题目"
    )
    item = actions.add_parser("add")
    item.add_argument("session_id")
    item = actions.add_parser("chat")
    item.add_argument("key", help="完整题目标识，如 leetcode_cn:25")
    item = actions.add_parser("rate")
    item.add_argument("session_id")
    item.add_argument("rating", choices=["again", "hard", "good"])
    item = sub.add_parser("test", help="验证本地代码文件，不调用模型或保存学习会话")
    item.add_argument("reference", help="内置题号 1 或 25")
    item.add_argument("path", type=Path)
    item = sub.add_parser("import", help="导入本地题目 JSON")
    item.add_argument("path", type=Path)
    item = sub.add_parser("chat", help="选择题目开始学习")
    item.add_argument("reference", help="题号、leetcode25 或本地已收录题目链接")
    source_options(item, select=True)
    item.add_argument(
        "--site", choices=["leetcode_cn", "leetcode_com", "local"], default="leetcode_cn"
    )
    item = sub.add_parser("resume", help="恢复已有学习会话")
    item.add_argument("session_id")
    item = sub.add_parser("status", help="查看会话状态，不调用模型")
    item.add_argument("session_id")
    item = sub.add_parser("archive", help="预览或应用归档，不调用模型")
    item.add_argument("session_id")
    item.add_argument("directory", type=Path)
    options = item.add_mutually_exclusive_group()
    options.add_argument("--apply", metavar="PLAN_ID", help="应用此前预览的完整计划 ID")
    options.add_argument("--show", metavar="FILE", help="显示当前预览中的一个文件")
    for name in ("commit", "push"):
        item = sub.add_parser(name, help=f"预览 {name}；--apply 计划 ID 明确执行")
        item.add_argument("session_id")
        item.add_argument("--apply", metavar="PLAN_ID")
    return root


def read_code(command: str) -> str | None:
    filename = command[len("/code") :].strip().strip('"')
    if filename:
        path = Path(filename).expanduser()
        if path.stat().st_size > MAX_CODE_BYTES:
            raise ValueError("代码文件不能超过 128 KiB")
        return path.read_bytes().decode("utf-8")
    print("粘贴代码；.end 结束，.cancel 取消。代码只做静态检查。")
    lines = []
    size = 0
    while True:
        try:
            line = input()
        except EOFError:
            print("代码输入未结束，本次粘贴未保存。")
            return None
        if line == ".cancel":
            return None
        if line == ".end":
            return "\n".join(lines)
        size += len(line.encode("utf-8")) + 1
        if size > MAX_CODE_BYTES:
            raise ValueError("代码不能超过 128 KiB；本次粘贴已取消")
        lines.append(line)


def configured_practice(value: Path | None) -> Path:
    if value is not None:
        return value
    directory = os.getenv("ALGOCOACH_PRACTICE_DIR", "").strip()
    if not directory:
        raise ValueError("请配置 ALGOCOACH_PRACTICE_DIR 或使用 /archive preview 绝对目录")
    return Path(directory)


def archive_command(coach: Coach, session_id: str, text: str, practice: Path | None):
    command, _, argument = text.partition(" ")
    if command == "apply":
        print(
            json.dumps(
                coach.apply_archive(session_id, argument.strip() or None), ensure_ascii=False
            )
        )
    elif command == "show":
        plan = coach.archive_plan(session_id)
        if argument not in plan.contents:
            raise ValueError("文件不在归档清单中")
        print(plan.contents[argument])
    elif not command or command == "preview":
        directory = (
            Path(argument.strip().strip('"')) if argument.strip() else configured_practice(practice)
        )
        print(coach.preview_archive(session_id, directory).preview())
    else:
        raise ValueError(
            "使用 /archive、/archive preview 目录、/archive show 文件名 或 /archive apply"
        )


def show_reviews(coach: Coach, day=None):
    day = day or today()
    items = coach.reviews.due(day)
    print(f"截至 {day} 的到期复习（Asia/Shanghai 用户日历）：")
    if not items:
        print("暂无到期题目；可使用 /review add 主动加入当前题。")
    for item in items:
        print(f"{item['problem_key']}\t{item['title']}\t到期：{item['due_date']}")


def inline_source(text):
    words = text.split()
    offline, refresh = "--offline" in words, "--refresh" in words
    values = [word for word in words if word not in ("--offline", "--refresh")]
    if len(values) > 1 or (offline and refresh):
        raise ValueError("只接受一个题号/链接或日期；--offline 和 --refresh 互斥")
    return (values[0] if values else ""), {"offline": offline, "refresh": refresh}


def conversation(coach: Coach, state: dict, practice: Path | None = None) -> None:
    session_id = state["session_id"]
    print(f"会话 ID：{session_id}；模式：{state['mode']}")
    print(state["response"])
    print("输入 /help 查看命令；退出后可使用 resume 恢复。")
    due = coach.reviews.due(today())
    if due:
        print(f"有 {len(due)} 道题到期，可用 /review 查看。")
    while True:
        try:
            text = input("\n你 > ").strip()
        except EOFError:
            print("\n已退出，学习记录保存在本地。")
            return
        if not text:
            continue
        try:
            if text in ("/quit", "/exit"):
                print(
                    f"已保存。恢复命令：algo-coach {'--demo ' if state['mode'] == 'demo' else ''}"
                    f"resume {session_id}"
                )
                return
            if text == "/help":
                print(HELP)
            elif text == "/status":
                print(coach.snapshot(session_id))
            elif text == "/test":
                state = coach.test(session_id)
                print(state["response"])
            elif text.startswith("/note "):
                state = coach.note(session_id, text[len("/note ") :])
                print("用户反思已保存；归档时将保留原文。")
            elif text.startswith("/explain "):
                state = coach.explain(session_id, text[len("/explain ") :])
                print("当前代码的用户思路已保存。")
            elif text == "/summarize":
                state = coach.summarize(session_id)
                print("以下是模型整理草稿，待你审阅：")
                print(
                    json.dumps(state["solution_summary"]["content"], ensure_ascii=False, indent=2)
                )
            elif text.startswith("/complete "):
                state = coach.complete(session_id, text[len("/complete ") :].strip())
                print("完成程度已记录：" + state["completion_mode"])
            elif text == "/archive" or text.startswith("/archive "):
                archive_command(coach, session_id, text[len("/archive") :].strip(), practice)
            elif text == "/commit" or text.startswith("/commit apply"):
                if text == "/commit":
                    result = coach.preview_commit(session_id)
                else:
                    result = coach.apply_commit(
                        session_id, text[len("/commit apply") :].strip() or None
                    )
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif text == "/push" or text.startswith("/push apply"):
                if text == "/push":
                    result = coach.preview_push(session_id)
                else:
                    result = coach.apply_push(
                        session_id, text[len("/push apply") :].strip() or None
                    )
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif text.startswith("/fetch "):
                reference, options = inline_source(text[len("/fetch ") :])
                state = coach.start_remote(reference, **options)
                session_id = state["session_id"]
                print(f"新远程会话 ID：{session_id}\n{state['response']}")
            elif text == "/daily fetch" or text.startswith("/daily fetch "):
                value, options = inline_source(text[len("/daily fetch") :])
                state = coach.start_remote_daily(parse_date(value) if value else None, **options)
                session_id = state["session_id"]
                print(f"新远程每日题会话 ID：{session_id}\n{state['response']}")
            elif text.startswith("/problem "):
                problem = coach.problems.resolve(text[len("/problem ") :])
                state = coach.start(problem)
                session_id = state["session_id"]
                print(f"新会话 ID：{session_id}\n{state['response']}")
            elif text == "/review":
                show_reviews(coach)
            elif text == "/review add":
                result = coach.add_review(session_id)
                print(f"已在复习队列：{result['problem_key']}；到期：{result['due_date']}")
            elif text.startswith("/review start "):
                state = coach.start_review(text[len("/review start ") :].strip())
                session_id = state["session_id"]
                print(f"新复习会话 ID：{session_id}\n{state['response']}")
            elif text.startswith("/review rate "):
                result = coach.rate_review(session_id, text[len("/review rate ") :].strip())
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif text == "/code" or text.startswith("/code "):
                code = read_code(text)
                if code is not None:
                    state = coach.turn(session_id, "请分析我提交的代码，不要执行。", code=code)
                    print(state["response"])
            elif text in ("/daily", "今日一题", "今天的每日一题") or text.startswith("/daily "):
                day = (
                    parse_date(text[len("/daily ") :].strip())
                    if text.startswith("/daily ")
                    else today()
                )
                state = coach.start_daily(day, state["problem"]["site"])
                session_id = state["session_id"]
                print(f"新日期题单会话 ID：{session_id}\n{state['response']}")
            else:
                state = coach.turn(session_id, text)
                print(state["response"])
        except ValidationError:
            print("输入数据未通过结构校验，请检查题目 JSON 的字段、长度和来源链接。")
        except (ValueError, OSError) as exc:
            print(f"未完成：{exc}")


def main(argv: list[str] | None = None) -> int:
    # PowerShell pipes may otherwise encode Chinese as the Windows legacy code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    # Load only this project's working-directory .env, not arbitrary parent directories.
    load_dotenv(Path.cwd() / ".env", override=False)
    args = parser().parse_args(argv)
    data_dir = args.data_dir or Path(os.getenv("ALGOCOACH_DATA_DIR", ".algo-coach"))
    try:
        if args.command == "doctor":
            print(json.dumps(DockerRunner.from_env().doctor(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "test":
            # No model credentials or session lock are needed for standalone verification.
            problem = LocalProblems(data_dir).resolve(args.reference)
            code = read_code(f'/code "{args.path}"')
            report = VerificationService().verify(problem, code)
            print(render_report(report.model_dump()))
            return 0 if report.status == "passed" else 1
        needs_model = not args.demo and (
            args.command in ("chat", "resume")
            or (args.command == "daily" and args.daily_action == "chat")
            or (args.command == "review" and args.review_action == "chat")
        )
        teacher = ModelTeacher() if needs_model else DemoTeacher()
        with Coach(data_dir, teacher) as coach:
            if args.command == "problems":
                for problem in coach.problems.all():
                    print(f"{problem.key}\t{problem.title}")
            elif args.command == "fetch":
                result = coach.remote.fetch(
                    args.reference, offline=args.offline, refresh=args.refresh
                )
                print(result.render() + "\n" + result.snapshot.problem.render())
            elif args.command == "daily":
                day = args.date or today()
                remote = args.daily_action == "fetch" or getattr(args, "remote", False)
                if args.daily_action != "set" and (args.offline or args.refresh) and not remote:
                    raise ValueError("离线/刷新选项针对远程缓存，请添加 --remote")
                if remote:
                    if args.site != "leetcode_cn":
                        raise ValueError("当前远程 Provider 仅支持中国站 leetcode_cn")
                    selection, fetched = coach.remote.daily(
                        day, offline=args.offline, refresh=args.refresh
                    )
                    if args.daily_action == "chat":
                        if teacher.mode == "model":
                            print("模型模式：后续题目、对话与代码将发送给配置的模型服务。")
                        state = coach.start(
                            selection.problem,
                            daily_selection=selection.model_dump(mode="json"),
                            problem_source=fetched.record(),
                        )
                        conversation(coach, state, args.practice_dir)
                    else:
                        print(fetched.render() + "\n" + selection.render())
                    return 0
                if args.daily_action == "set":
                    from algo_coach.service import now

                    problem = coach.problems.resolve(args.reference, args.site)
                    selection = coach.daily.set(
                        problem,
                        day,
                        now(),
                        platform=args.platform,
                        source_url=args.source_url,
                        replace=args.replace,
                    )
                    print(selection.render())
                elif args.daily_action == "show":
                    print(coach.daily.get(day, args.site).render())
                else:
                    state = coach.start_daily(day, args.site)
                    if teacher.mode == "model":
                        print("模型模式：后续相关对话与代码会发送给你配置的模型服务。")
                    conversation(coach, state, args.practice_dir)
            elif args.command == "review":
                if args.review_action == "list":
                    show_reviews(coach, args.date)
                elif args.review_action == "add":
                    result = coach.add_review(args.session_id)
                    print(
                        json.dumps(
                            {k: v for k, v in result.items() if k != "problem"},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif args.review_action == "rate":
                    print(
                        json.dumps(
                            coach.rate_review(args.session_id, args.rating),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    state = coach.start_review(args.key)
                    if teacher.mode == "model":
                        print("模型模式：后续相关对话与代码会发送给你配置的模型服务。")
                    conversation(coach, state, args.practice_dir)
            elif args.command == "import":
                problem = coach.problems.import_file(args.path)
                print(f"已导入 {problem.key}：{problem.title}")
            elif args.command == "sessions":
                for session_id, title, mode, created in coach.sessions():
                    print(f"{session_id}\t{mode}\t{title}\t{created}")
            elif args.command == "status":
                print(coach.snapshot(args.session_id))
            elif args.command == "archive":
                if args.apply:
                    plan = coach.archive_plan(args.session_id, args.apply)
                    if args.directory.resolve() != Path(plan.root):
                        raise ValueError("目标目录与此前预览不一致")
                    print(
                        json.dumps(
                            coach.apply_archive(args.session_id, args.apply), ensure_ascii=False
                        )
                    )
                else:
                    plan = coach.preview_archive(args.session_id, args.directory)
                    if args.show:
                        if args.show not in plan.contents:
                            raise ValueError("文件不在预览清单中")
                        print(plan.contents[args.show])
                    else:
                        print(plan.preview())
            elif args.command in ("commit", "push"):
                action = getattr(coach, ("apply_" if args.apply else "preview_") + args.command)
                result = (
                    action(args.session_id, args.apply) if args.apply else action(args.session_id)
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == "chat":
                if teacher.mode == "model":
                    print("模型模式：题目、对话及所提交代码会发送给你配置的模型服务。")
                if args.remote:
                    if args.site != "leetcode_cn":
                        raise ValueError("当前远程 Provider 仅支持中国站 leetcode_cn")
                    state = coach.start_remote(
                        args.reference, offline=args.offline, refresh=args.refresh
                    )
                else:
                    if args.offline or args.refresh:
                        raise ValueError("离线/刷新选项针对远程缓存，请添加 --remote")
                    state = coach.start(coach.problems.resolve(args.reference, args.site))
                conversation(coach, state, args.practice_dir)
            elif args.command == "resume":
                state = coach.get(args.session_id)
                if state["mode"] != teacher.mode:
                    raise ValueError("会话模式不匹配；演示会话需 --demo，真实模型会话不要加此参数")
                if teacher.mode == "model":
                    print("模型模式：后续相关对话与代码会发送给你配置的模型服务。")
                conversation(coach, state, args.practice_dir)
        return 0
    except Timeout:
        print("此数据目录正在被另一个 AlgoCoach 进程使用，请先退出另一进程。", file=sys.stderr)
    except ValidationError:
        print("题目数据校验失败；请参考 examples/problems/sum-local.json。", file=sys.stderr)
    except ImportError:
        print("缺少模型供应商集成包，请安装对应 LangChain 适配器。", file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(f"无法完成：{exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n已中断，已完成的学习步骤保存在本地。", file=sys.stderr)
        return 130
    return 1
