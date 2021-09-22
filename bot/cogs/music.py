import asyncio
import datetime as dt
import enum
import random
import re
import typing as t
from enum import Enum
from itertools import cycle

import aiohttp
import discord
from discord.ext.commands.core import command
import wavelink
from discord.ext import commands

status = cycle(["*도움"])
URL_REGEX = r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'\".,<>?«»“”‘’]))"
LYRICS_URL = "https://some-random-api.ml/lyrics?title="
HZ_BANDS = (20, 40, 63, 100, 150, 250, 400, 450, 630, 1000, 1600, 2500, 4000, 10000, 16000)
TIME_REGEX = r"([0-9]{1,2})[:ms](([0-9]{1,2})s?)?"
OPTIONS = {
    "1️⃣": 0,
    "2⃣": 1,
    "3⃣": 2,
    "4⃣": 3,
    "5⃣": 4,
}


class AlreadyConnectedToChannel(commands.CommandError):
    pass


class NoVoiceChannel(commands.CommandError):
    pass


class QueueIsEmpty(commands.CommandError):
    pass


class NoTracksFound(commands.CommandError):
    pass


class PlayerIsAlreadyPaused(commands.CommandError):
    pass


class NoMoreTracks(commands.CommandError):
    pass


class NoPreviousTracks(commands.CommandError):
    pass


class InvalidRepeatMode(commands.CommandError):
    pass


class VolumeTooLow(commands.CommandError):
    pass


class VolumeTooHigh(commands.CommandError):
    pass


class MaxVolume(commands.CommandError):
    pass


class MinVolume(commands.CommandError):
    pass


class NoLyricsFound(commands.CommandError):
    pass


class InvalidEQPreset(commands.CommandError):
    pass


class NonExistentEQBand(commands.CommandError):
    pass


class EQGainOutOfBounds(commands.CommandError):
    pass


class InvalidTimeString(commands.CommandError):
    pass


class RepeatMode(Enum):
    NONE = 0
    ONE = 1
    ALL = 2


class Queue:
    def __init__(self):
        self._queue = []
        self.position = 0
        self.repeat_mode = RepeatMode.NONE

    @property
    def is_empty(self):
        return not self._queue

    @property
    def current_track(self):
        if not self._queue:
            raise QueueIsEmpty

        if self.position <= len(self._queue) - 1:
            return self._queue[self.position]

    @property
    def upcoming(self):
        if not self._queue:
            raise QueueIsEmpty

        return self._queue[self.position + 1:]

    @property
    def history(self):
        if not self._queue:
            raise QueueIsEmpty

        return self._queue[:self.position]

    @property
    def length(self):
        return len(self._queue)

    def add(self, *args):
        self._queue.extend(args)

    def get_next_track(self):
        if not self._queue:
            raise QueueIsEmpty

        self.position += 1

        if self.position < 0:
            return None
        elif self.position > len(self._queue) - 1:
            if self.repeat_mode == RepeatMode.ALL:
                self.position = 0
            else:
                return None

        return self._queue[self.position]

    def shuffle(self):
        if not self._queue:
            raise QueueIsEmpty

        upcoming = self.upcoming
        random.shuffle(upcoming)
        self._queue = self._queue[:self.position + 1]
        self._queue.extend(upcoming)

    def set_repeat_mode(self, mode):
        if mode == "없음":
            self.repeat_mode = RepeatMode.NONE
        elif mode == "현재":
            self.repeat_mode = RepeatMode.ONE
        elif mode == "전체":
            self.repeat_mode = RepeatMode.ALL

    def empty(self):
        self._queue.clear()
        self.position = 0


class Player(wavelink.Player):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = Queue()
        self.eq_levels = [0.] * 15

    async def connect(self, ctx, channel=None):
        if self.is_connected:
            raise AlreadyConnectedToChannel

        if (channel := getattr(ctx.author.voice, "channel", channel)) is None:
            raise NoVoiceChannel

        await super().connect(channel.id)
        return channel

    async def teardown(self):
        try:
            await self.destroy()
        except KeyError:
            pass

    async def add_tracks(self, ctx, tracks):
        if not tracks:
            raise NoTracksFound

        if isinstance(tracks, wavelink.TrackPlaylist):
            self.queue.add(*tracks.tracks)
        elif len(tracks) == 1:
            self.queue.add(tracks[0])
            await ctx.send(f"{tracks[0].title} 가 재생목록에 추가되었습니다.")
        else:
            if (track := await self.choose_track(ctx, tracks)) is not None:
                self.queue.add(track)
                await ctx.send(f"{track.title} 가 재생목록에 추가되었습니다.")

        if not self.is_playing and not self.queue.is_empty:
            await self.start_playback()

    async def choose_track(self, ctx, tracks):
        def _check(r, u):
            return (
                r.emoji in OPTIONS.keys()
                and u == ctx.author
                and r.message.id == msg.id
            )

        embed = discord.Embed(
            title="노래를 선택해주세요.(이모지 클릭)",
            description=(
                "\n".join(
                    f"**{i+1}.** {t.title} ({t.length//60000}:{str(t.length%60).zfill(2)})"
                    for i, t in enumerate(tracks[:5])
                )
            ),
            colour=ctx.author.colour,
            timestamp=dt.datetime.utcnow()
        )
        embed.set_author(name="검색 결과입니다.")        

        msg = await ctx.send(embed=embed)
        for emoji in list(OPTIONS.keys())[:min(len(tracks), len(OPTIONS))]:
            await msg.add_reaction(emoji)

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=60.0, check=_check)
        except asyncio.TimeoutError:
            await msg.delete()
            await ctx.message.delete()
        else:
            await msg.delete()
            return tracks[OPTIONS[reaction.emoji]]

    async def start_playback(self):
        await self.play(self.queue.current_track)

    async def advance(self):
        try:
            if (track := self.queue.get_next_track()) is not None:
                await self.play(track)
        except QueueIsEmpty:
            pass

    async def repeat_track(self):
        await self.play(self.queue.current_track)


class Music(commands.Cog, wavelink.WavelinkMixin):
    def __init__(self, bot):
        self.bot = bot
        self.wavelink = wavelink.Client(bot=bot)
        self.bot.loop.create_task(self.start_nodes())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not member.bot and after.channel is None:
            if not [m for m in before.channel.members if not m.bot]:
                await self.get_player(member.guild).teardown()

    @wavelink.WavelinkMixin.listener()
    async def on_node_ready(self, node):
        print(f" Wavelink node `{node.identifier}` ready.")

    @wavelink.WavelinkMixin.listener("on_track_stuck")
    @wavelink.WavelinkMixin.listener("on_track_end")
    @wavelink.WavelinkMixin.listener("on_track_exception")
    async def on_player_stop(self, node, payload):
        if payload.player.queue.repeat_mode == RepeatMode.ONE:
            await payload.player.repeat_track()
        else:
            await payload.player.advance()

    async def cog_check(self, ctx):
        if isinstance(ctx.channel, discord.DMChannel):
            await ctx.send("Music commands are not available in DMs.")
            return False

        return True

    async def start_nodes(self):
        await self.bot.wait_until_ready()

        nodes = {"MAIN": 
            {
                "host": "lavalink1122.herokuapp.com",
                "port": 80,
                "rest_url": "https://lavalink1122.herokuapp.com/",
                "password": "youshallnotpass",
                "identifier": "MAIN",
                "region": "europe"
            }
        }

        for node in nodes.values():
            await self.wavelink.initiate_node(**node)

    def get_player(self, obj):
        if isinstance(obj, commands.Context):
            return self.wavelink.get_player(obj.guild.id, cls=Player, context=obj)
        elif isinstance(obj, discord.Guild):
            return self.wavelink.get_player(obj.id, cls=Player)

    @commands.command(name="도움")
    async def help_command(self, ctx):
        player = self.get_player(ctx)
        embed = discord.Embed(title="뮤직봇 사용방법 입니다." , descrioption = "용도에 맞는 명령어를 입력해주세요.", color=0x00ff00)
        embed.add_field(name="+연결", value="음성채널에 입장한 뒤 입력해주세요.", inline=False)
        embed.add_field(name="+해제", value="뮤직봇 사용종료 후 입력해주세요.", inline=False)
        embed.add_field(name="+재생", value="URL을 입력시 자동재생, 검색어 입력시 이모지 클릭. 예시) +재생 문리버 ", inline=False)
        embed.add_field(name="+일시정지", value="현재 재생되는 곡을 일시정지 합니다. 다시 재생하고 싶으면 +재생", inline=False)
        embed.add_field(name="+정지", value="재생되는 곡 정지 및 재생 목록을 초기화 합니다.", inline=False)
        embed.add_field(name="+목록", value="재생목록을 보여줍니다.", inline=False)
        embed.add_field(name="+다음", value="다음 곡을 재생합니다.", inline=False)
        embed.add_field(name="+이전", value="이전 곡을 재생합니다.", inline=False)
        embed.add_field(name="+셔플", value="재생목록을 셔플합니다.", inline=False)
        embed.add_field(name="+반복", value="재생목록을 반복합니다. 예시) +반복 없음, +반복 현재, +반복 전체", inline=False)
        embed.add_field(name="+볼륨 숫자", value="볼륨을 조절합니다. 예시) +볼륨 30 ", inline=False)
        embed.add_field(name="+볼륨 업", value="볼륨을 10% 올려줍니다.", inline=False)
        embed.add_field(name="+볼륨 다운", value="볼륨을 10% 내려줍니다..", inline=False)
        embed.add_field(name="+현재곡", value="현재 재생되는 곡의 정보를 보여줍니다.", inline=False)
        embed.add_field(name="+스킵 숫자", value="원하는 순서의 곡을 재생해줍니다. 예시) +스킵 2(목록에서 두번째 곡 재생)", inline=False)
        embed.add_field(name="+재시작", value="현재 재생되는 곡을 재시작 합니다.", inline=False)
        embed.add_field(name="+이동 숫자", value="현재 재생되는 곡의 원하는 시간부터 재생해줍니다. 예시) +이동 1:00", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="연결", aliases=["join"])
    async def connect_command(self, ctx, *, channel: t.Optional[discord.VoiceChannel]):
        player = self.get_player(ctx)
        channel = await player.connect(ctx, channel)
        await ctx.send(f"뮤직봇과 연결되었습니다.")

    @connect_command.error
    async def connect_command_error(self, ctx, exc):
        if isinstance(exc, AlreadyConnectedToChannel):
            await ctx.send("이미 음성채널에 들어와있습니다.")
        elif isinstance(exc, NoVoiceChannel):
            await ctx.send("음성채널에 입장해주세요.")

    @commands.command(name="해제", aliases=["leave"])
    async def disconnect_command(self, ctx):
        player = self.get_player(ctx)
        await player.teardown()
        await ctx.send("뮤직봇과의 연결이 해제되었습니다.")

    @commands.command(name="재생")
    async def play_command(self, ctx, *, query: t.Optional[str]):
        player = self.get_player(ctx)

        if not player.is_connected:
            await player.connect(ctx)

        if query is None:
            if player.queue.is_empty:
                raise QueueIsEmpty

            await player.set_pause(False)
            await ctx.send("재생이 재개되었습니다.")

        else:
            query = query.strip("<>")
            if not re.match(URL_REGEX, query):
                query = f"ytsearch:{query}"

            await player.add_tracks(ctx, await self.wavelink.get_tracks(query))

    @play_command.error
    async def play_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("재생목록이 비어있습니다.")
        elif isinstance(exc, NoVoiceChannel):
            await ctx.send("음성채널에 입장해주세요.")

    @commands.command(name="일시정지")
    async def pause_command(self, ctx):
        player = self.get_player(ctx)

        if player.is_paused:
            raise PlayerIsAlreadyPaused

        await player.set_pause(True)
        await ctx.send("일시정지 되었습니다.")

    @pause_command.error
    async def pause_command_error(self, ctx, exc):
        if isinstance(exc, PlayerIsAlreadyPaused):
            await ctx.send("이미 일시정지 되었습니다.")

    @commands.command(name="정지")
    async def stop_command(self, ctx):
        player = self.get_player(ctx)
        player.queue.empty()
        await player.stop()
        await ctx.send("정지 되었습니다.")

    @commands.command(name="다음", aliases=["skip"])
    async def next_command(self, ctx):
        player = self.get_player(ctx)

        if not player.queue.upcoming:
            raise NoMoreTracks

        await player.stop()
        await ctx.send("다음 곡을 재생합니다.")

    @next_command.error
    async def next_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("재생목록이 비어있으므로 실행할 수 없습니다.")
        elif isinstance(exc, NoMoreTracks):
            await ctx.send("재생목록에 더 이상 곡이 없습니다.")

    @commands.command(name="이전")
    async def previous_command(self, ctx):
        player = self.get_player(ctx)

        if not player.queue.history:
            raise NoPreviousTracks

        player.queue.position -= 2
        await player.stop()
        await ctx.send("이전 곡을 재생합니다.")

    @previous_command.error
    async def previous_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("재생목록이 비어있으므로 실행할 수 없습니다.")
        elif isinstance(exc, NoPreviousTracks):
            await ctx.send("재생목록에 더 이상 곡이 없습니다.")

    @commands.command(name="셔플")
    async def shuffle_command(self, ctx):
        player = self.get_player(ctx)
        player.queue.shuffle()
        await ctx.send("셔플되었습니다.")

    @shuffle_command.error
    async def shuffle_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("재생목록이 비어있으므로 셔플할 수 없습니다.")

    @commands.command(name="반복")
    async def repeat_command(self, ctx, mode: str):
        if mode not in ("없음", "현재", "전체"):
            raise InvalidRepeatMode

        player = self.get_player(ctx)
        player.queue.set_repeat_mode(mode)
        await ctx.send(f"{mode} 반복모드가 설정되었습니다.")

    @commands.command(name="목록")
    async def queue_command(self, ctx, show: t.Optional[int] = 10):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        embed = discord.Embed(
            title="재생목록",
            description=f"다음 {show}곡의 재생목록 입니다.",
            colour=ctx.author.colour,
            timestamp=dt.datetime.utcnow()
        )
        embed.set_author(name="재생목록 입니다.")
        embed.add_field(
            name="현재 재생중인 곡",
            value=getattr(player.queue.current_track, "제목", "현재 재생중인 노래가 없습니다."),
            inline=False
        )
        if upcoming := player.queue.upcoming:
            embed.add_field(
                name="다음 노래",
                value="\n".join(t.title for t in upcoming[:show]),
                inline=False
            )

        msg = await ctx.send(embed=embed)

    @queue_command.error
    async def queue_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("재생목록이 현재 비어있습니다.")

    # Requests -----------------------------------------------------------------

    @commands.group(name="볼륨", invoke_without_command=True)
    async def volume_group(self, ctx, volume: int):
        player = self.get_player(ctx)

        if volume < 0:
            raise VolumeTooLow

        if volume > 150:
            raise VolumeTooHigh

        await player.set_volume(volume)
        await ctx.send(f"{volume:,}% 로 볼륨이 조절되었습니다.")

    @volume_group.error
    async def volume_group_error(self, ctx, exc):
        if isinstance(exc, VolumeTooLow):
            await ctx.send("볼륨을 0% 이상으로 조절해주세요.")
        elif isinstance(exc, VolumeTooHigh):
            await ctx.send("볼륨을 150% 이하로 조절해주세요.")

    @volume_group.command(name="업")
    async def volume_up_command(self, ctx):
        player = self.get_player(ctx)

        if player.volume == 150:
            raise MaxVolume

        await player.set_volume(value := min(player.volume + 10, 150))
        await ctx.send(f"{value:,}% 로 볼륨이 조절되었습니다. 10%씩 증가, 최대 150%")

    @volume_up_command.error
    async def volume_up_command_error(self, ctx, exc):
        if isinstance(exc, MaxVolume):
            await ctx.send("최고 볼륨입니다.")

    @volume_group.command(name="다운")
    async def volume_down_command(self, ctx):
        player = self.get_player(ctx)

        if player.volume == 0:
            raise MinVolume

        await player.set_volume(value := max(0, player.volume - 10))
        await ctx.send(f"{value:,}% 로 볼륨이 조절되었습니다. 10%씩 감소, 최대 0%")

    @volume_down_command.error
    async def volume_down_command_error(self, ctx, exc):
        if isinstance(exc, MinVolume):
            await ctx.send("최소 볼륨입니다.")


    @commands.command(name="현재곡", aliases=["np"])
    async def playing_command(self, ctx):
        player = self.get_player(ctx)

        if not player.is_playing:
            raise PlayerIsAlreadyPaused

        embed = discord.Embed(
            title="현재 재생중인 곡",
            colour=ctx.author.colour,
            timestamp=dt.datetime.utcnow(),
        )
        embed.set_author(name="재생중인 곡 정보입니다.")
        embed.add_field(name="제목", value=player.queue.current_track.title, inline=False)
        embed.add_field(name="가수", value=player.queue.current_track.author, inline=False)

        position = divmod(player.position, 60000)
        length = divmod(player.queue.current_track.length, 60000)
        embed.add_field(
            name="재생 시간(현재/전체)",
            value=f"{int(position[0])}:{round(position[1]/1000):02}/{int(length[0])}:{round(length[1]/1000):02}",
            inline=False
        )

        await ctx.send(embed=embed)

    @playing_command.error
    async def playing_command_error(self, ctx, exc):
        if isinstance(exc, PlayerIsAlreadyPaused):
            await ctx.send("현재 재생 중인 곡이 없습니다.")

    @commands.command(name="스킵", aliases=["playindex"])
    async def skipto_command(self, ctx, index: int):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        if not 0 <= index <= player.queue.length:
            raise NoMoreTracks

        player.queue.position = index - 2
        await player.stop()
        await ctx.send(f"재생목록 중 {index}번째 곡을 재생합니다.")

    @skipto_command.error
    async def skipto_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("재생목록에 곡이 없습니다.")
        elif isinstance(exc, NoMoreTracks):
            await ctx.send("재생목록 범위를 넘어갔습니다.")

    @commands.command(name="재시작")
    async def restart_command(self, ctx):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        await player.seek(0)
        await ctx.send("곡이 다시 재생되었습니다.")

    @restart_command.error
    async def restart_command_error(self, ctx, exc):
        if isinstance(exc, QueueIsEmpty):
            await ctx.send("재생목록에 곡이 없습니다.")

    @commands.command(name="이동")
    async def seek_command(self, ctx, position: str):
        player = self.get_player(ctx)

        if player.queue.is_empty:
            raise QueueIsEmpty

        if not (match := re.match(TIME_REGEX, position)):
            raise InvalidTimeString

        if match.group(3):
            secs = (int(match.group(1)) * 60) + (int(match.group(3)))
        else:
            secs = int(match.group(1))

        await player.seek(secs * 1000)
        await ctx.send("이동했습니다.")


def setup(bot):
    bot.add_cog(Music(bot))
