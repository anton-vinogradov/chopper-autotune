# chopper-autotune

**Полностью автоматический подбор chopper-регистров TMC-драйверов для Klipper по измерениям на реальном железе.**

[English version](README.md)

> **Статус: стадия проектирования.** Этот документ — описание намерений. Рабочего кода пока нет: методика измерений проверена проектами-предшественниками (см. ниже), этот проект автоматизирует принятие решений вокруг неё.

## Проблема

Значения chopper-регистров (`TBL`, `TOFF`, `HSTRT`, `HEND`, `TPFD`) сильно влияют на поведение шагового мотора: до ~30% разницы в моменте, до 10 раз — в вибрациях, плюс слышимый шум. Оптимум зависит от конкретного мотора, драйвера, напряжения питания и механики — заводские значения из даташита являются компромиссом.

Существующие инструменты оставляют разрыв:

- [chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) и [tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — полный перебор сетки регистров (~7000 комбинаций, ~2 часа, ~700 МБ CSV), после чего **человек** глазами выбирает минимум на интерактивном графике. В лучшем случае полуавтоматика.
- [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — считает регистры аналитически из базы моторов, **вообще без обратной связи от железа**.

## Намерение

Инструмент, замыкающий цикл на реальном железе: *применить регистры → прогнать ось → измерить вибрации акселерометром на голове → оценить → выбрать следующего кандидата*. Полностью автоматически: от «запустил одну команду» до «вставь этот блок в `printer.cfg`».

### Планируемый процесс

1. **Baseline** — замер уровня шума акселерометра в покое.
2. **Поиск резонансной скорости** — прогон по скоростям с текущим конфигом; пики находятся автоматически (`scipy.signal.find_peaks`), а не человеком по HTML-графику.
3. **Поиск регистров** — оптимизация по дискретному пространству регистров на резонансной скорости(ях). Покоординатный спуск в порядке, рекомендованном Trinamic в [AN-001](https://www.analog.com/en/app-notes/AN-001.html) (`TBL`+`TOFF` → `HSTRT`/`HEND` → `TPFD`), и/или байесовская оптимизация (Optuna/TPE). Шум измерений компенсируется successive halving: дешёвые одиночные прогоны для разведки, повторные — для подтверждения лидеров. CSV обрабатываются и удаляются инкрементально — требование 700 МБ на диске снимается.
4. **Валидация и выдача** — перемер топ-кандидатов на 2–3 скоростях в обе стороны, готовый блок для `printer.cfg`, опционально — применение на лету через `SET_TMC_FIELD`.

Ожидаемое время работы: десятки минут вместо часов, либо тот же бюджет времени на многоскоростную целевую функцию.

### Оценка по даташитам, а не только по измерениям

Акселерометр не «слышит» чоппер (ADXL345 сэмплирует 3.2 кГц), но частота чоппера *вычислима* из регистров и клока драйвера. Это делает классический компромисс «вибрации низкие, но противный высокочастотный писк» автоматическим: кандидаты, у которых частота чоппера попадает в слышимый диапазон, штрафуются аналитически.

Также по даташитам:

- ограничения пространства поиска (`HSTRT`+`HEND` ≤ 16, запрет `TOFF` = 0, ограничения blank time для `TOFF` = 1) — отсекаются до какого-либо движения;
- матрица возможностей по драйверам (`TPFD` только на TMC2240/5160; принудительный spreadCycle на TMC2208/2209 на время теста);
- в перспективе: чтение StallGuard как прокси запаса момента для автоподбора тока мотора (тише и холоднее при известном запасе).

## Планируемый стек

Python 3 на хосте принтера. Moonraker API для оркестрации (без Jinja-циклов в макросах), `numpy`/`scipy` для PSD и поиска пиков, хелперы `shaper_calibrate` самого Klipper где применимо, Optuna для поиска, plotly для итогового отчёта.

## Требования

- Klipper + Moonraker
- Акселерометр на печатающей голове ([Measuring Resonances](https://www.klipper3d.org/Measuring_Resonances.html))
- Поддерживаемый TMC-драйвер (см. ниже)

## Дорожная карта

- [ ] Moonraker-клиент + измерительный примитив (применить регистры, `FORCE_MOVE`, инкрементальная обработка CSV)
- [ ] Автоматический поиск резонансной скорости
- [ ] Покоординатный оптимизатор с ограничениями из даташитов
- [ ] Модель частоты чоппера и штраф за слышимый диапазон
- [ ] Поиск на Optuna как альтернативная стратегия
- [ ] Фаза валидации и выдача блока `printer.cfg`
- [ ] Поддержка TPFD (TMC2240/5160)
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
