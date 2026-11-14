"""ArchiMate figure geometry ported from Archi 5.10.0.

Source: https://github.com/archimatetool/archi, tag ``release_5.10.0``,
``com.archimatetool.editor/src/com/archimatetool/editor/diagram/figures/**``
(element figures, figure delegates, ``diagram/NoteFigure``, ``diagram/GroupFigure``).
The Java was read as data and transliterated; nothing of Archi is executed.

----------------------------------------------------------------------------
The MIT License (MIT)

Copyright (c) 2013-2026 Phillip Beauvoir, Jean-Baptiste Sarrodie, The Open Group

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
----------------------------------------------------------------------------
"""

from __future__ import annotations

from tools.rendering.archi.figures_a import SPECS as _SPECS_A
from tools.rendering.archi.figures_b import SPECS as _SPECS_B
from tools.rendering.archi.spec import FigureSpec

FIGURES: dict[str, FigureSpec] = {**_SPECS_A, **_SPECS_B}

# Registry concept type → Archi figure class (the edit part each UIProvider
# creates in com.archimatetool.editor.ui.factory.elements).
TYPE_FIGURE: dict[str, str] = {
    "resource": "ResourceFigure",
    "capability": "CapabilityFigure",
    "value_stream": "ValueStreamFigure",
    "course_of_action": "CourseOfActionFigure",
    "business_actor": "BusinessActorFigure",
    "business_role": "BusinessRoleFigure",
    "business_collaboration": "CollaborationFigure",
    "business_interface": "InterfaceFigure",
    "business_process": "ProcessFigure",
    "business_function": "FunctionFigure",
    "business_interaction": "InteractionFigure",
    "business_event": "EventFigure",
    "business_service": "ServiceFigure",
    "business_object": "ObjectFigure",
    "contract": "ContractFigure",
    "representation": "RepresentationFigure",
    "product": "ProductFigure",
    "application_component": "ApplicationComponentFigure",
    "application_collaboration": "CollaborationFigure",
    "application_interface": "InterfaceFigure",
    "application_function": "FunctionFigure",
    "application_interaction": "InteractionFigure",
    "application_process": "ProcessFigure",
    "application_event": "EventFigure",
    "application_service": "ServiceFigure",
    "data_object": "ObjectFigure",
    "node": "NodeFigure",
    "device": "DeviceFigure",
    "system_software": "SystemSoftwareFigure",
    "technology_collaboration": "CollaborationFigure",
    "technology_interface": "InterfaceFigure",
    "path": "PathFigure",
    "communication_network": "CommunicationNetworkFigure",
    "technology_function": "FunctionFigure",
    "technology_process": "ProcessFigure",
    "technology_interaction": "InteractionFigure",
    "technology_event": "EventFigure",
    "technology_service": "ServiceFigure",
    "artifact": "ArtifactFigure",
    "equipment": "EquipmentFigure",
    "facility": "FacilityFigure",
    "distribution_network": "DistributionNetworkFigure",
    "material": "MaterialFigure",
    "stakeholder": "StakeholderFigure",
    "driver": "DriverFigure",
    "assessment": "AssessmentFigure",
    "goal": "GoalFigure",
    "outcome": "OutcomeFigure",
    "principle": "PrincipleFigure",
    "requirement": "RequirementFigure",
    "constraint": "ConstraintFigure",
    "meaning": "MeaningFigure",
    "value": "ValueFigure",
    "work_package": "WorkPackageFigure",
    "deliverable": "DeliverableFigure",
    "implementation_event": "EventFigure",
    "plateau": "PlateauFigure",
    "gap": "GapFigure",
    "location": "LocationFigure",
    "grouping": "GroupingFigure",
    "junction": "JunctionFigure",
}

__all__ = ["FIGURES", "TYPE_FIGURE", "FigureSpec"]
