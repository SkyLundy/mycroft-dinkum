/*
 * Copyright 2018 by Marco Martin <mart@kde.org>
 * Copyright 2018 David Edmundson <davidedmundson@kde.org>
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

import QtQuick 2.9
import Mycroft 1.0 as Mycroft

SliderBase {
    id: root
    iconSource: Qt.resolvedUrl("./audio-volume-high.svg")

    slider.from: 0
    slider.to: 1

    slider.value: 0.6
    slider.onMoved: {
        Mycroft.MycroftController.sendRequest("mycroft.volume.set", {"percent": slider.value});
    }

    Component.onCompleted: {
        Mycroft.MycroftController.sendRequest("mycroft.mic.get_status", {});
    }

    Connections {
        target: Mycroft.MycroftController
        onSocketStatusChanged: {
            if (Mycroft.MycroftController.status == Mycroft.MycroftController.Open) {
                Mycroft.MycroftController.sendRequest("mycroft.volume.get", {});
            }
        }
        onIntentRecevied: {
            if (type == "mycroft.volume.get.response") {
                slider.value = data.percent;
            }
            if (type == "hardware.volume"){
                slider.value = data.volume;
            }
        }
    }

}
