# chopper-autotune

**Полностью автоматический подбор chopper-регистров TMC-драйверов для Klipper по измерениям на реальном железе.**

[English version](README.md)

[![tests](https://github.com/anton-vinogradov/chopper-autotune/actions/workflows/ci.yml/badge.svg)](https://github.com/anton-vinogradov/chopper-autotune/actions/workflows/ci.yml)

> **Статус: рабочий скелет.** `collect` и `analyze` реализованы (сначала полный перебор сетки, умный поиск следующим шагом), на реальном железе пока не проверено.

## Проблема

Значения chopper-регистров (`TBL`, `TOFF`, `HSTRT`, `HEND`, `TPFD`) сильно влияют на поведение шагового мотора: до ~30% разницы в моменте, до 10 раз — в вибрациях, плюс слышимый шум. Оптимум зависит от конкретного мотора, драйвера, напряжения питания и механики — заводские значения из даташита являются компромиссом.

Существующие инструменты оставляют разрыв:

- [chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) и [tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — полный перебор сетки регистров (~7000 комбинаций, ~2 часа, ~700 МБ CSV), после чего **человек** глазами выбирает минимум на интерактивном графике. В лучшем случае полуавтоматика.
- [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — считает регистры аналитически из базы моторов, **вообще без обратной связи от железа**.

## Подход

Замкнуть цикл на реальном железе: *применить регистры → прогнать ось → измерить вибрации акселерометром на голове → оценить → выбрать следующего кандидата*. Полностью автоматически: от «запустил одну команду» до «вставь этот блок в `printer.cfg`».

### Как это работает сейчас

1. **`find-speed`** проходит диапазон скоростей на текущих регистрах, строит кривую «магнитуда(скорость)», находит резонансные пики (по prominence) и рекомендует скорость для основного прогона.
2. **`collect`** читает всё необходимое из конфига принтера через API-сокет klippy (тип драйвера, текущие регистры, акселерометр, кинематику, границы осей), строит план по сетке регистров/скоростей с отсечением по ограничениям из даташитов, проверяет длину хода против диапазона оси, печатает ETA и просит подтверждение.
3. Принтер выполняет хоуминг XY, паркуется в центре стола и отключает моторы. Для каждой комбинации применяются регистры через `SET_TMC_FIELD`, выполняется `FORCE_MOVE` туда-обратно, а сэмплы акселерометра стримятся напрямую из сокета klippy. Конец каждого движения берётся из `toolhead.print_time`, поэтому в метрику попадает ровно крейсерская фаза — разгон и торможение отрезаются аналитически, а не на глаз.
4. Каждое измерение сразу дописывается в датасет на диске; прерванный прогон возобновляется с места остановки.
5. **`analyze`** агрегирует датасет (медиана по направлениям/итерациям/скоростям), штрафует конфигурации с частотой чоппера в слышимом диапазоне, печатает таблицу ранжирования, пишет интерактивный plotly-отчёт и готовый блок для `printer.cfg`; `--apply` применяет победителя на лету без рестарта Klipper.

Кроме полного перебора сетки (по умолчанию), `--search descent` (`SEARCH=descent`) запускает покоординатный спуск в порядке настройки из AN-001 — `TBL`+`TOFF` совместно, затем `HSTRT`, `HEND`, затем `TPFD` — оценивая единицы процентов сетки (минуты вместо часов) и перемеряя топ-кандидатов перед рекомендацией. В целевую функцию входит штраф за слышимый чоппер, так что спуск не разменяет чуть меньшую вибрацию на писк 15 кГц. Для второй оси `SEED_FROM=<датасет>` стартует спуск с победителя первой — подсказка только позиционирует поиск, каждый кандидат всё равно измеряется на целевой оси, так что разница натяжения ремней и механики учитывается; хорошая подсказка сходится за пару минут, плохая просто стоит обычного времени спуска. Любой записанный grid-датасет — офлайн-полигон: `simulate <dataset>` проигрывает спуск по нему и показывает отставание от истинного оптимума.

### Оценка по даташитам, а не только по измерениям

Акселерометр не «слышит» чоппер (ADXL345 сэмплирует 3.2 кГц), но частота чоппера *вычислима* из регистров и клока драйвера. Это делает классический компромисс «вибрации низкие, но противный высокочастотный писк» автоматическим: кандидаты с частотой чоппера в слышимом диапазоне штрафуются аналитически (`--audible-weight`).

Также по даташитам:

- ограничения пространства поиска (`HSTRT`+`HEND` ≤ 16, запрет `TOFF` = 0, ограничения blank time для `TOFF` = 1) — отсекаются до какого-либо движения;
- матрица возможностей по драйверам: `TPFD` попадает в сетку только на TMC2240/5160, частоты клока сверены с кодом драйверов Klipper;
- при настроенном `stealthchop_threshold` на время теста принудительно включается spreadCycle с восстановлением после — chopper-регистры действуют только в spreadCycle, в stealthChop мерился бы шум;
- в планах: чтение StallGuard как прокси запаса момента для автоподбора тока мотора.

## Два запуска by design

Инструмент сознательно разделён на две команды, работающие с одним датасетом на диске (`manifest.json` + `measurements.jsonl` + сжатые сырые CSV акселерометра):

1. **`collect`** — медленная часть на железе. Стримит сэмплы из API-сокета klippy (никакой возни с CSV в `/tmp` и износа SD-карты; `--csv` — fallback на классический путь через `ACCELEROMETER_MEASURE`). Прерванный или расширенный прогон возобновляется из той же директории датасета: готовые измерения пропускаются.
2. **`analyze`** — офлайн и мгновенно. Сырые данные сохраняются в датасете, поэтому скоринг можно переделывать и перепроигрывать (`--recompute`) не трогая принтер.

Умные стратегии поиска позже поселятся внутри `collect` и будут выбирать следующую точку онлайн, но датасет остаётся append-only и полным — анализ по-прежнему воспроизводим офлайн.

## Использование

Установка на хосте принтера (в конце Klipper перезапустится):

```
cd ~ && git clone https://github.com/anton-vinogradov/chopper-autotune && bash ./chopper-autotune/install.sh
```

Дальше из веб-консоли (Mainsail/Fluidd):

```
CHOPPER_FIND_SPEED                   ; 1. найти резонансные скорости оси
CHOPPER_COLLECT SPEED=55 DRY_RUN=1   ; посмотреть план и ETA, ничего не двигая
CHOPPER_COLLECT SPEED=55             ; 2. полный перебор сетки на резонансе (часы)
CHOPPER_COLLECT SPEED=55 SEARCH=descent  ; ...или покоординатный спуск (минуты)
CHOPPER_COLLECT AXIS=Y SPEED=52 SEARCH=descent SEED_FROM=<датасет X>  ; быстрая вторая ось
CHOPPER_STATUS                       ; прогресс и ETA идущего сбора
CHOPPER_ANALYZE                      ; 3. ранжировать свежий датасет, построить отчёт
CHOPPER_ANALYZE APPLY=1              ; применить победителя на лету через SET_TMC_FIELD
CHOPPER_ANALYZE SAVE=1               ; вписать в конфиг и перезапустить Klipper
```

То же самое по SSH: `chopper-autotune collect --axis x --speed 55`, `chopper-autotune analyze [dir]`. Каждый параметр макроса отображается 1:1 во флаг CLI (`MEASURE_TIME=1.5` → `--measure-time 1.5`).

Прогресс дублируется на экран принтера (KlipperScreen / LCD / шапка веб-интерфейса) через `M117`, финальная рекомендация остаётся на экране. Датасеты и HTML-отчёты складываются в `~/printer_data/config/chopper-autotune/datasets/` — видны в файловом менеджере веб-интерфейса. Сетка сужается через `TBL/TOFF/HSTRT/HEND/TPFD=lo:hi` (например, `CHOPPER_COLLECT SPEED=55 TOFF=1:8 HEND=0:7`), прерванный прогон возобновляется передачей его директории в `DATASET=`. `collect` должен запускаться на хосте принтера (общается с unix-сокетом klippy); `analyze` — где угодно. `APPLY=1` живёт до перезагрузки; `SAVE=1` переписывает строки `driver_*` нужной секции в конфиге (предварительно делается копия `.chopper-backup.cfg`) и перезапускает Klipper. `uninstall.sh` убирает интеграцию, датасеты остаются.

## Стек

Python 3.9+ на хосте принтера. API-сокет klippy для оркестрации и стриминга сэмплов (без Jinja-циклов в макросах; Moonraker HTTP — только для `analyze --apply`), `numpy` для метрик, plotly для отчётов; поиск пиков через `scipy` и Optuna-поиск — в планах.

## Требования

- Klipper + Moonraker (Mainsail/Fluidd или любой другой фронтенд)
- Акселерометр на печатающей голове ([Measuring Resonances](https://www.klipper3d.org/Measuring_Resonances.html))
- Поддерживаемый TMC-драйвер (см. ниже)

## Дорожная карта

- [x] Двухфазный дизайн: `collect` (железо, возобновляемый датасет) / `analyze` (офлайн, воспроизводимо)
- [x] Измерительный примитив поверх API-сокета klippy (регистры → `FORCE_MOVE` → стрим сэмплов)
- [x] Перебор сетки с ограничениями из даташитов, TPFD на TMC2240/5160
- [x] Модель частоты чоппера и штраф за слышимый диапазон (первое приближение)
- [x] Макросы веб-консоли (`CHOPPER_COLLECT`/`CHOPPER_ANALYZE`), инсталлятор, update_manager Moonraker
- [x] Стриминг сэмплов с точной нарезкой крейсерской фазы (`--csv` fallback)
- [x] Проверка на реальном принтере (CoreXY, TMC2209, ADXL345: стрим и CSV-путь согласуются)
- [x] Автоматический поиск резонансной скорости (`find-speed`, пики по prominence)
- [x] Принудительный spreadCycle на время теста при настроенном `stealthchop_threshold`; `CHOPPER_STATUS` прогресс/ETA
- [x] Покоординатный спуск (`--search descent`: порядок AN-001, штраф за писк в целевой функции, перемер топ-3, офлайн-replay через `simulate`)
- [ ] Optuna/TPE-стратегия, ранний abort плохих кандидатов посреди движения
- [ ] Фаза валидации (перемер топ-кандидатов перед рекомендацией)
- [ ] Автоподбор тока по StallGuard

## Предшественники и благодарности

- [MRX8024/chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) — оригинальная методика измерений
- [anton-vinogradov/tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — упрощённый форк, прямой предшественник
- [andrewmcgr/klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — аналитический подход (без измерений)
- Trinamic [AN-001: Parameterization of spreadCycle](https://www.analog.com/en/app-notes/AN-001.html)

## Даташиты

- TMC2130 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2130_datasheet_rev1.15.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2130)
- TMC2208 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2202_TMC2208_TMC2224_datasheet_rev1.14.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2208)
- TMC2209 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2209_datasheet_rev1.09.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2209)
- TMC2660 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2660C_Datasheet_Rev1.01.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2660)
- TMC2240 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/tmc2240_datasheet.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2240)
- TMC5160 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC5160A_datasheet_rev1.17.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc5160)

## Лицензия

[MIT](LICENSE.TXT)
