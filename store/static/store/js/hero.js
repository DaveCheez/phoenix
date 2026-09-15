/* DISPLAY A ALTERNATING HEADING */
// Construct an array of headers
const textArr = ['Roof Racks', 'Side Ladders','Spare Wheel Carriers','Rear Door Ladders','Side Steps...']

for (i=0; i<100; i++) {
    for (heading in textArr) {
        rotateText(heading)
    }
}
function rotateText(heading) {
    setTimeout(function () {
            document.getElementById("rotating-text").innerHTML = textArr[heading];
            console.log(textArr[heading]);
        }, 3000 * heading);
    }
